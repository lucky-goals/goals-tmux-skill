"""Bounded, explicit-socket transport for the tmux command line client."""

from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import time
import math
from typing import Sequence

from .errors import DomainError


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class Tmux:
    """Run tmux commands against one explicitly selected server socket."""

    def __init__(
        self,
        socket_path: str,
        binary: str | None = None,
        timeout: float = 5,
        config_file: str | None = None,
    ) -> None:
        if not isinstance(socket_path, str) or not socket_path:
            raise ValueError("socket_path must be a non-empty string")
        try:
            normalized_timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout must be a finite positive number") from exc
        if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
            raise ValueError("timeout must be positive")
        self.socket_path = socket_path
        self.binary = binary or shutil.which("tmux") or "tmux"
        self.timeout = normalized_timeout
        self.config_file = config_file

    def _argv(self, args: Sequence[str], *, mutating: bool) -> list[str]:
        argv = [self.binary]
        if self.config_file:
            argv.extend(("-f", self.config_file))
        argv.extend(("-S", self.socket_path))
        if not mutating:
            # -N is supported by the project's 3.6a baseline and prevents
            # read-only probes from starting a server when the socket is absent.
            argv.append("-N")
        argv.extend(args)
        return argv

    @staticmethod
    def _cleanup_process(proc: subprocess.Popen[bytes], selector: selectors.BaseSelector) -> None:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            selector.close()
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _communicate_bounded(
        self, argv: Sequence[str], input_bytes: bytes | None, *, mutating: bool
    ) -> tuple[bytes, bytes, int]:
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        try:
            proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=env,
                close_fds=True,
            )
        except OSError as exc:
            raise DomainError(
                "TMUX_EXEC_FAILED",
                f"Could not start tmux: {exc}",
                status="refused",
                side_effects="none",
            ) from exc

        selector = selectors.DefaultSelector()
        stdout = bytearray()
        stderr = bytearray()
        streams = {"stdout": proc.stdout, "stderr": proc.stderr}
        total_written = 0
        input_view = memoryview(input_bytes or b"")
        input_offset = 0
        deadline = time.monotonic() + self.timeout

        try:
            for name, stream in streams.items():
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)

            if input_bytes is not None:
                assert proc.stdin is not None
                os.set_blocking(proc.stdin.fileno(), False)
                if len(input_view):
                    selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    proc.stdin.close()

            while selector.get_map() or proc.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, self.timeout)

                events = selector.select(min(remaining, 0.1))
                for key, _mask in events:
                    stream = key.fileobj
                    kind = key.data
                    if kind == "stdin":
                        try:
                            count = os.write(stream.fileno(), input_view[input_offset : input_offset + 65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            count = 0
                        if count == 0:
                            selector.unregister(stream)
                            try:
                                stream.close()
                            except OSError:
                                pass
                        else:
                            input_offset += count
                            if input_offset >= len(input_view):
                                selector.unregister(stream)
                                try:
                                    stream.close()
                                except OSError:
                                    pass
                    else:
                        try:
                            chunk = os.read(stream.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(stream)
                            try:
                                stream.close()
                            except OSError:
                                pass
                            continue
                        total_written += len(chunk)
                        if total_written > MAX_RESPONSE_BYTES:
                            raise DomainError(
                                "TMUX_RESPONSE_TOO_LARGE",
                                "tmux response exceeded the 2 MiB limit",
                                status="unknown",
                                side_effects="unknown",
                            )
                        (stdout if kind == "stdout" else stderr).extend(chunk)

            returncode = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            return bytes(stdout), bytes(stderr), returncode
        except subprocess.TimeoutExpired as exc:
            self._cleanup_process(proc, selector)
            raise DomainError(
                "TMUX_TIMEOUT",
                "tmux command timed out",
                status="unknown" if mutating else "refused",
                side_effects="unknown" if mutating else "none",
            ) from exc
        except DomainError as exc:
            self._cleanup_process(proc, selector)
            if exc.code == "TMUX_RESPONSE_TOO_LARGE":
                # A mutating command may already have run before its output
                # exceeded the bound, so its result is necessarily unknown.
                raise DomainError(
                    exc.code,
                    str(exc),
                    status="unknown" if mutating else "refused",
                    side_effects="unknown" if mutating else "none",
                ) from exc
            raise
        except (OSError, ValueError) as exc:
            self._cleanup_process(proc, selector)
            raise DomainError(
                "TMUX_IO_FAILED",
                f"tmux communication failed: {exc}",
                status="unknown" if mutating else "refused",
                side_effects="unknown" if mutating else "none",
            ) from exc
        finally:
            try:
                selector.close()
            except Exception:
                pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    @staticmethod
    def _classify_failure(stderr: str) -> tuple[str, str]:
        lowered = stderr.lower()
        if "permission denied" in lowered or "operation not permitted" in lowered:
            return "TMUX_PERMISSION_DENIED", "tmux could not access the selected socket"
        if "no server running" in lowered or (
            "error connecting" in lowered and ("no such file" in lowered or "connection refused" in lowered)
        ):
            return "TMUX_SERVER_UNAVAILABLE", "No tmux server is available at the selected socket"
        return "TMUX_COMMAND_FAILED", "tmux command failed"

    def run(
        self,
        *args: str,
        input: bytes | None = None,
        mutating: bool = False,
        check: bool = True,
    ) -> str:
        """Run one tmux command and return stdout without trimming whitespace."""
        if not args or any(not isinstance(arg, str) or "\0" in arg for arg in args):
            raise DomainError("INVALID_TMUX_ARGUMENT", "tmux arguments must be non-empty strings without NUL")
        if input is not None and not isinstance(input, bytes):
            raise DomainError("INVALID_TMUX_INPUT", "tmux stdin must be bytes")

        argv = self._argv(args, mutating=mutating)
        stdout_bytes, stderr_bytes, returncode = self._communicate_bounded(argv, input, mutating=mutating)
        try:
            stdout = stdout_bytes.decode("utf-8")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
        except UnicodeDecodeError as exc:
            raise DomainError(
                "TMUX_INVALID_UTF8",
                "tmux returned stdout that was not valid UTF-8",
                status="unknown" if mutating else "refused",
                side_effects="unknown" if mutating else "none",
            ) from exc

        if check and returncode != 0:
            code, message = self._classify_failure(stderr)
            if mutating:
                raise DomainError(code, message, status="unknown", side_effects="unknown")
            raise DomainError(code, message, status="refused", side_effects="none", data={"returncode": returncode})
        return stdout

    def version(self) -> str:
        """Return the local tmux version without connecting to a server."""
        return self.run("-V", mutating=False).rstrip("\n")

    def commands(self) -> str:
        """Return tmux's command table without connecting to a server."""
        return self.run("list-commands", mutating=False)
