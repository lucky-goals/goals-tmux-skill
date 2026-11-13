"""Isolated git worktree provisioning and concurrent-writer guards (execution layer).

A writing run (``permission_profile == "bypass"``) can isolate its working tree
under ``<state_dir>/worktrees/<task_id>/<nickname>/`` on branch
``gt/<task_id>/<nickname>``. Concurrent ``bypass`` runs without ``worktree``
isolation are refused with ``WORKTREE_CONFLICT`` when they share a resolved
``cwd``, unless ``allow_shared_cwd=True`` is requested.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import errors
from .errors import PlatformError
from .fsutil import private_dir

_GIT_ENV_STRIP = frozenset({
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
})


@dataclass(frozen=True)
class WorktreeInfo:
    """Metadata describing a provisioned run worktree."""

    path: Path
    branch: str
    repo_root: Path
    source_cwd: Path
    base_commit: str

    def to_dict(self) -> dict[str, object]:
        """Serialize worktree metadata for ``run.json`` and ``state.json``."""
        return {
            "path": str(self.path),
            "branch": self.branch,
            "repo_root": str(self.repo_root),
            "source_cwd": str(self.source_cwd),
            "base_commit": self.base_commit,
        }

    def event_payload(self) -> dict[str, object]:
        """Payload recorded on ``worktree_prepared`` events."""
        return {
            "path": str(self.path),
            "branch": self.branch,
            "repo_root": str(self.repo_root),
            "base_commit": self.base_commit,
        }


def _git_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _GIT_ENV_STRIP}


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            env=_git_env(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlatformError(errors.WORKTREE_FAILED, f"git {' '.join(args)} failed: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit status {proc.returncode}"
        raise PlatformError(errors.WORKTREE_FAILED, f"git {' '.join(args)} failed: {detail}")
    return proc


def _validate_component(value: str, label: str) -> None:
    if (
        not value
        or "/" in value
        or "\\" in value
        or value in (".", "..")
        or ".." in value
        or value.startswith("-")
    ):
        raise PlatformError(errors.WORKTREE_FAILED, f"invalid {label} for worktree path: {value!r}")


def verify_git_repo(cwd: str | Path) -> tuple[Path, str]:
    """Verify ``cwd`` lies inside a git repository with a valid ``HEAD`` commit."""
    source = Path(cwd).resolve()
    top = _run_git(source, "rev-parse", "--show-toplevel").stdout.strip()
    if not top:
        raise PlatformError(errors.WORKTREE_FAILED, f"{source} is not inside a git repository")
    repo_root = Path(top).resolve()
    base_commit = _run_git(repo_root, "rev-parse", "--verify", "HEAD").stdout.strip()
    return repo_root, base_commit


@contextmanager
def creation_lock(state_dir: Path) -> Iterator[None]:
    """Serialize shared-cwd checks and run record creation under ``runs/create.lock``."""
    runs_dir = Path(state_dir) / "runs"
    private_dir(runs_dir)
    fd = os.open(runs_dir / "create.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def ensure_cwd_available(
    state_dir: Path,
    cwd: str | Path,
    *,
    stage_profiles: dict[str, str],
) -> None:
    """Refuse a non-worktree ``bypass`` run if an active run already uses ``cwd``."""
    runs_dir = Path(state_dir) / "runs"
    if not runs_dir.is_dir():
        return
    target = Path(cwd).resolve()
    for state_path in sorted(runs_dir.glob("*/state.json")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run = json.loads((state_path.parent / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if state.get("state") == "closed":
            continue
        if run.get("worktree"):
            continue
        profile = run.get("permission_profile") or stage_profiles.get(run.get("stage", ""))
        if profile != "bypass":
            continue
        run_cwd = run.get("cwd")
        if not isinstance(run_cwd, str) or not run_cwd:
            continue
        if Path(run_cwd).resolve() == target:
            raise PlatformError(
                errors.WORKTREE_CONFLICT,
                f"active bypass run {run.get('nickname') or run.get('run_id')} already uses {target}",
                cwd=str(target),
                conflicting_run_id=run.get("run_id"),
                conflicting_nickname=run.get("nickname"),
            )


def prepare_worktree(
    state_dir: Path,
    cwd: str | Path,
    task_id: str,
    nickname: str,
) -> WorktreeInfo:
    """Provision ``<state_dir>/worktrees/<task_id>/<nickname>/`` on ``gt/<task_id>/<nickname>``."""
    _validate_component(task_id, "task_id")
    _validate_component(nickname, "nickname")
    repo_root, base_commit = verify_git_repo(cwd)
    source_cwd = Path(cwd).resolve()
    worktree_path = (Path(state_dir) / "worktrees" / task_id / nickname).resolve()
    branch = f"gt/{task_id}/{nickname}"
    if worktree_path.exists():
        raise PlatformError(
            errors.WORKTREE_FAILED,
            f"worktree path already exists: {worktree_path}",
            path=str(worktree_path),
            branch=branch,
        )
    private_dir(worktree_path.parent)
    _run_git(repo_root, "worktree", "add", "-b", branch, str(worktree_path), "HEAD")
    try:
        os.chmod(worktree_path, 0o700)
    except OSError:
        pass
    return WorktreeInfo(
        path=worktree_path,
        branch=branch,
        repo_root=repo_root,
        source_cwd=source_cwd,
        base_commit=base_commit,
    )


def remove_worktree(metadata: dict[str, object], *, force: bool = False) -> dict[str, object]:
    """Remove a provisioned worktree and its branch, refusing dirty trees unless ``force=True``."""
    raw_path = metadata.get("path")
    branch = metadata.get("branch")
    raw_repo = metadata.get("repo_root")
    if not isinstance(raw_path, str) or not isinstance(branch, str) or not isinstance(raw_repo, str):
        raise PlatformError(errors.WORKTREE_FAILED, "incomplete worktree metadata in run record")
    worktree_path = Path(raw_path)
    repo_root = Path(raw_repo)

    if worktree_path.exists():
        if not force:
            status = _run_git(worktree_path, "status", "--porcelain")
            if status.stdout.strip():
                raise PlatformError(
                    errors.WORKTREE_DIRTY,
                    f"worktree has uncommitted changes: {worktree_path}",
                    path=str(worktree_path),
                    branch=branch,
                )
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree_path))
        proc = _run_git(repo_root, *args, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"exit status {proc.returncode}"
            if not force and ("modified or untracked" in detail or "dirty" in detail or "--force" in detail):
                raise PlatformError(
                    errors.WORKTREE_DIRTY,
                    f"worktree has uncommitted changes: {detail}",
                    path=str(worktree_path),
                    branch=branch,
                )
            raise PlatformError(
                errors.WORKTREE_FAILED,
                f"git worktree remove failed: {detail}",
                path=str(worktree_path),
                branch=branch,
            )
    elif not force:
        raise PlatformError(
            errors.WORKTREE_FAILED,
            f"worktree directory does not exist: {worktree_path}",
            path=str(worktree_path),
            branch=branch,
        )
    else:
        _run_git(repo_root, "worktree", "prune")

    if _run_git(repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        _run_git(repo_root, "branch", "-D", branch)

    try:
        worktree_path.parent.rmdir()
    except OSError:
        pass

    return {
        "path": str(worktree_path),
        "branch": branch,
        "repo_root": str(repo_root),
        "force": bool(force),
    }
