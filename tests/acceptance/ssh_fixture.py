"""Isolated loopback OpenSSH server for bounded acceptance checks.

The fixture owns every file and process it creates. It never reads or edits a
user's SSH configuration or keys and binds only to 127.0.0.1.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Iterator


SSHD = "/usr/sbin/sshd"
SSH = "/usr/bin/ssh"
SSH_KEYGEN = "/usr/bin/ssh-keygen"


class SSHFixtureError(RuntimeError):
    """The local OpenSSH fixture could not be prepared or started."""


@dataclass(frozen=True)
class IsolatedSSHServer:
    """Paths and connection details for one private loopback SSH server.

    ``config_path`` is the client configuration for ``ssh -F``. The separate
    ``server_config_path`` configures only this fixture's ``sshd`` process.
    """

    root: Path
    port: int
    config_path: Path
    server_config_path: Path
    known_hosts_path: Path
    user_private_key_path: Path
    authorized_keys_path: Path
    alias: str
    username: str
    process: subprocess.Popen[bytes]

    def ssh_argv(self, *remote_command: str) -> list[str]:
        """Return an ssh command pinned to this fixture's client config."""

        if any(not isinstance(part, str) or not part or "\0" in part for part in remote_command):
            raise ValueError("remote command arguments must be nonempty strings without NUL")
        return [SSH, "-F", str(self.config_path), self.alias, *remote_command]

    def run(self, *remote_command: str, timeout: float = 10.0) -> subprocess.CompletedProcess[bytes]:
        """Run one command on this fixture using only the generated SSH identity."""

        if not remote_command:
            raise ValueError("a remote command is required")
        env = os.environ.copy()
        for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "DISPLAY"):
            env.pop(name, None)
        env["LC_ALL"] = "C"
        return subprocess.run(
            self.ssh_argv(*remote_command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            check=False,
        )


def _require_binaries() -> None:
    missing = [path for path in (SSHD, SSH, SSH_KEYGEN) if not Path(path).is_file() or not os.access(path, os.X_OK)]
    if missing:
        raise SSHFixtureError("required OpenSSH binary is unavailable: " + ", ".join(missing))


def _username() -> str:
    try:
        name = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError) as exc:
        raise SSHFixtureError(f"could not resolve the current account for AllowUsers: {exc}") from exc
    if not name or any(char.isspace() or char in "\r\n\x00" for char in name):
        raise SSHFixtureError("the current account name is not safe for the isolated sshd configuration")
    return name


def _generate_key(path: Path, comment: str) -> None:
    result = subprocess.run(
        [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise SSHFixtureError(f"ssh-keygen failed for the isolated fixture: {detail or result.returncode}")
    os.chmod(path, 0o600)


def _public_key_fields(path: Path) -> tuple[str, str]:
    try:
        fields = path.read_text(encoding="ascii").strip().split()
    except (OSError, UnicodeDecodeError) as exc:
        raise SSHFixtureError(f"could not read generated public key {path.name}: {exc}") from exc
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise SSHFixtureError(f"ssh-keygen produced an unexpected public key in {path.name}")
    return fields[0], fields[1]


def _ephemeral_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    if port < 1024:
        raise SSHFixtureError(f"the OS selected a non-high ephemeral port: {port}")
    return port


def _server_config(
    *, port: int, username: str, host_key: Path, authorized_keys: Path, pid_file: Path
) -> str:
    return "\n".join(
        (
            f"Port {port}",
            "AddressFamily inet",
            "ListenAddress 127.0.0.1",
            f"HostKey {host_key}",
            f"PidFile {pid_file}",
            f"AuthorizedKeysFile {authorized_keys}",
            f"AllowUsers {username}",
            # macOS resolves /tmp to /private/tmp, a shared sticky parent that
            # StrictModes rejects even though this fixture's own root is 0700.
            # All key/config files live under that private owner-only root.
            "StrictModes no",
            "PubkeyAuthentication yes",
            "AuthenticationMethods publickey",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "ChallengeResponseAuthentication no",
            "UsePAM no",
            "PermitRootLogin no",
            "PermitEmptyPasswords no",
            "PermitUserEnvironment no",
            "AllowAgentForwarding no",
            "AllowTcpForwarding local",
            "PermitOpen 127.0.0.1:*",
            "GatewayPorts no",
            "X11Forwarding no",
            "PermitTunnel no",
            "UseDNS no",
            "PrintMotd no",
            "LogLevel VERBOSE",
            "",
        )
    )


def _client_config(*, port: int, username: str, private_key: Path, known_hosts: Path, alias: str) -> str:
    return "\n".join(
        (
            f"Host {alias}",
            "    HostName 127.0.0.1",
            f"    Port {port}",
            f"    User {username}",
            f"    IdentityFile {private_key}",
            "    IdentitiesOnly yes",
            "    IdentityAgent none",
            f"    UserKnownHostsFile {known_hosts}",
            "    GlobalKnownHostsFile /dev/null",
            "    StrictHostKeyChecking yes",
            "    UpdateHostKeys no",
            "    BatchMode yes",
            "    PreferredAuthentications publickey",
            "    PasswordAuthentication no",
            "    KbdInteractiveAuthentication no",
            "    ForwardAgent no",
            "    ConnectTimeout 3",
            "    LogLevel ERROR",
            "",
        )
    )


def _log_tail(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return content[-6000:].strip()


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


@contextmanager
def isolated_ssh_server(*, startup_timeout: float = 5.0) -> Iterator[IsolatedSSHServer]:
    """Start an ephemeral-key OpenSSH server bound only to loopback.

    The process runs as the current user without sudo. If the platform's sshd
    requires elevated privileges, startup fails with its actual diagnostic and
    the fixture leaves system SSH files untouched.
    """

    if not isinstance(startup_timeout, (int, float)) or startup_timeout <= 0:
        raise ValueError("startup_timeout must be a positive number")
    _require_binaries()
    username = _username()
    alias = "goals-isolated-ssh"
    temporary = tempfile.TemporaryDirectory(prefix="goals-ssh-fixture-", dir="/tmp")
    root = Path(temporary.name)
    os.chmod(root, 0o700)
    process: subprocess.Popen[bytes] | None = None
    try:
        host_key = root / "host_ed25519"
        user_key = root / "user_ed25519"
        authorized_keys = root / "authorized_keys"
        known_hosts = root / "known_hosts"
        client_config = root / "ssh_config"
        server_config = root / "sshd_config"
        pid_file = root / "sshd.pid"
        log_file = root / "sshd.log"

        _generate_key(host_key, "goals isolated ssh host")
        _generate_key(user_key, "goals isolated ssh user")
        user_key_type, user_key_data = _public_key_fields(root / "user_ed25519.pub")
        host_key_type, host_key_data = _public_key_fields(root / "host_ed25519.pub")
        authorized_keys.write_text(f"{user_key_type} {user_key_data} goals isolated user\n", encoding="ascii")
        os.chmod(authorized_keys, 0o600)

        port = _ephemeral_loopback_port()
        known_hosts.write_text(
            f"[127.0.0.1]:{port} {host_key_type} {host_key_data}\n", encoding="ascii"
        )
        os.chmod(known_hosts, 0o600)
        client_config.write_text(
            _client_config(port=port, username=username, private_key=user_key,
                           known_hosts=known_hosts, alias=alias),
            encoding="utf-8",
        )
        os.chmod(client_config, 0o600)
        server_config.write_text(
            _server_config(port=port, username=username, host_key=host_key,
                           authorized_keys=authorized_keys, pid_file=pid_file),
            encoding="utf-8",
        )
        os.chmod(server_config, 0o600)

        checked = subprocess.run(
            [SSHD, "-t", "-f", str(server_config)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if checked.returncode != 0:
            detail = checked.stderr.decode("utf-8", "replace").strip()
            raise SSHFixtureError(
                "isolated sshd configuration validation failed without privilege escalation: "
                + (detail or f"exit status {checked.returncode}")
            )

        with log_file.open("ab", buffering=0) as log_stream:
            process = subprocess.Popen(
                [SSHD, "-D", "-e", "-f", str(server_config)],
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )

        deadline = time.monotonic() + float(startup_timeout)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                detail = _log_tail(log_file)
                raise SSHFixtureError(
                    f"isolated sshd exited during startup with status {process.returncode}: "
                    + (detail or "no sshd diagnostic was written")
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            detail = _log_tail(log_file)
            raise SSHFixtureError(
                f"isolated sshd did not listen on 127.0.0.1:{port} within {startup_timeout}s: "
                + (detail or "no sshd diagnostic was written")
            )

        yield IsolatedSSHServer(
            root=root,
            port=port,
            config_path=client_config,
            server_config_path=server_config,
            known_hosts_path=known_hosts,
            user_private_key_path=user_key,
            authorized_keys_path=authorized_keys,
            alias=alias,
            username=username,
            process=process,
        )
    finally:
        _terminate(process)
        temporary.cleanup()


if __name__ == "__main__":
    with isolated_ssh_server() as fixture:
        result = fixture.run("/usr/bin/true")
        if result.returncode != 0:
            raise SystemExit(
                "isolated ssh smoke failed: "
                + result.stderr.decode("utf-8", "replace").strip()
            )
        print(
            f"isolated ssh smoke passed: {fixture.alias} on 127.0.0.1:{fixture.port} "
            f"(sshd pid {fixture.process.pid})"
        )
