"""Interactive generator for a proposed Goals tmux workflow YAML file.

The wizard records user preferences only. The current runner does not load or
enforce the resulting YAML, and this module never launches a model session.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import difflib
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Callable, TextIO

from .workflow_template import new_workflow


DEFAULT_PROBE_TIMEOUT = 8.0
MIN_PROBE_TIMEOUT = 0.1
MAX_PROBE_TIMEOUT = 30.0
MAX_EXISTING_CONFIG_BYTES = 1024 * 1024
CLAUDE_MODEL_GUIDE = "https://code.claude.com/docs/en/model-config"
CLAUDE_MODEL_FALLBACK = (
    {
        "id": "claude-opus-5-5",
        "name": "Claude Opus 5.5",
        "efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    {
        "id": "claude-sonnet-5-5",
        "name": "Claude Sonnet 5.5",
        "efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    {
        "id": "claude-haiku-5-5",
        "name": "Claude Haiku 5.5",
        "efforts": ["low", "medium", "high", "xhigh", "max"],
    },
)
QUICK_EFFORTS = ("low", "medium", "high", "xhigh", "max")

CLI_DISPLAY_NAMES = {
    "claude": "Claude Code",
    "cursor": "Cursor",
    "codex": "Codex",
    "agy": "Antigravity CLI",
    "kiro": "Kiro CLI",
}

SCENARIOS = (
    ("research", "需求调研"),
    ("design", "需求设计"),
    ("test_design", "测试设计"),
    ("implementation", "需求实现"),
    ("testing", "需求测试"),
    ("ui_prototype_html", "UI 原型设计（HTML）"),
    ("ui_implementation", "UI 实现"),
    ("ui_automation", "UI 自动化"),
    ("review", "需求 Review"),
    ("other", "其他场景"),
)


class WizardCancelled(Exception):
    """Raised when the user declines to continue or input closes."""


class UnsafeTargetError(ValueError):
    """Raised when a destination is a symlink or not a regular file."""


class ConcurrentModificationError(RuntimeError):
    """Raised when a destination changes after the wizard inspected it."""


@dataclass(frozen=True)
class FileSnapshot:
    exists: bool
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    size: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    uid: int | None = None
    gid: int | None = None
    digest: str | None = None
    content: bytes | None = None


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


HOST_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_HOST_NAME_LENGTH = 64


def validate_host_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("Host 名称不能为空。")
    if len(cleaned) > MAX_HOST_NAME_LENGTH:
        raise ValueError(f"Host 名称长度不能超过 {MAX_HOST_NAME_LENGTH} 个字符。")
    if cleaned.startswith("."):
        raise ValueError("Host 名称不能以点开头。")
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned:
        raise ValueError("Host 名称不能包含路径分隔符或 \"..\"。")
    if not HOST_NAME_PATTERN.fullmatch(cleaned):
        raise ValueError(
            "Host 名称无效：必须以字母或数字开头，且仅包含字母、数字、点、下划线或连字符。"
        )
    return cleaned


def _xdg_config_base(environ: dict[str, str] | None = None, home: Path | None = None) -> Path:
    environment = os.environ if environ is None else environ
    configured = environment.get("XDG_CONFIG_HOME")
    if configured:
        base = Path(configured)
        if not base.is_absolute():
            raise ValueError("XDG_CONFIG_HOME 必须是绝对路径；请改正环境变量或选择项目配置。")
    else:
        base = (Path.home() if home is None else Path(home)) / ".config"
    return base


def user_config_destination(environ: dict[str, str] | None = None, home: Path | None = None) -> Path:
    """Return the XDG user workflow path, requiring an absolute XDG base."""

    base = _xdg_config_base(environ, home)
    return _absolute(base / "goals-tmux-skill" / "workflow.yaml")


def host_config_destination(
    environ: dict[str, str] | None = None,
    home: Path | None = None,
    host_name: str = "trae-cn",
) -> Path:
    """Return the XDG host workflow path, requiring an absolute XDG base and valid host name."""

    valid_name = validate_host_name(host_name)
    base = _xdg_config_base(environ, home)
    return _absolute(base / "goals-tmux-skill" / "hosts" / f"{valid_name}.yaml")


def project_destination(project_dir: Path) -> Path:
    """Return the workflow path in the selected project directory."""

    root = Path(project_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"项目目录不存在或不是目录：{root}")
    return root / ".goals-tmux.yaml"


def is_skill_installation_dir(project_dir: Path) -> bool:
    """Detect whether project_dir appears to be the skill installation directory."""

    try:
        resolved = Path(project_dir).expanduser().resolve()
    except Exception:
        resolved = Path(project_dir)
    if resolved.name == "goals-tmux-skill" or Path(project_dir).name == "goals-tmux-skill":
        return True
    skill_file = resolved / "SKILL.md"
    tmuxctl_file = resolved / "scripts" / "tmuxctl"
    try:
        skill_stat = os.lstat(skill_file)
        tmuxctl_stat = os.lstat(tmuxctl_file)
        if stat.S_ISREG(skill_stat.st_mode) and stat.S_ISREG(tmuxctl_stat.st_mode):
            return True
    except OSError:
        pass
    return False


def _read_regular_target(path: Path) -> FileSnapshot:
    """Read a regular target without following a final-component symlink."""

    try:
        path_info = os.lstat(path)
    except FileNotFoundError:
        return FileSnapshot(exists=False)
    except NotADirectoryError as exc:
        raise UnsafeTargetError(f"目标路径的父级不是目录：{path}") from exc

    if stat.S_ISLNK(path_info.st_mode):
        raise UnsafeTargetError(f"拒绝写入符号链接目标：{path}")
    if not stat.S_ISREG(path_info.st_mode):
        raise UnsafeTargetError(f"目标已存在，但不是普通文件：{path}")
    if path_info.st_size > MAX_EXISTING_CONFIG_BYTES:
        raise UnsafeTargetError(
            f"现有目标超过 {MAX_EXISTING_CONFIG_BYTES} 字节的安全检查上限，拒绝覆盖：{path}"
        )

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, os.O_RDONLY | no_follow)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeTargetError(f"拒绝写入符号链接目标：{path}") from exc
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise UnsafeTargetError(f"目标已存在，但不是普通文件：{path}")
        if (opened.st_dev, opened.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise ConcurrentModificationError(f"检查期间目标已变化：{path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(128 * 1024, MAX_EXISTING_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_EXISTING_CONFIG_BYTES:
                raise UnsafeTargetError(
                    f"现有目标超过 {MAX_EXISTING_CONFIG_BYTES} 字节的安全检查上限，拒绝覆盖：{path}"
                )
        after = os.fstat(fd)
    finally:
        os.close(fd)
    content = b"".join(chunks)
    before_identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if before_identity != after_identity or len(content) != after.st_size:
        raise ConcurrentModificationError(f"读取期间目标已变化：{path}")
    return FileSnapshot(
        exists=True,
        device=after.st_dev,
        inode=after.st_ino,
        mode=stat.S_IMODE(after.st_mode),
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        ctime_ns=after.st_ctime_ns,
        uid=after.st_uid,
        gid=after.st_gid,
        digest=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def inspect_destination(path: Path) -> FileSnapshot:
    """Inspect a destination without changing it."""

    return _read_regular_target(_absolute(Path(path)))


def _ensure_parent_directory(parent: Path) -> None:
    """Create missing parent directories privately, one level at a time."""

    parent = _absolute(parent)
    missing: list[Path] = []
    cursor = parent
    while True:
        try:
            info = os.lstat(cursor)
        except FileNotFoundError:
            missing.append(cursor)
            next_cursor = cursor.parent
            if next_cursor == cursor:
                raise
            cursor = next_cursor
            continue
        if not stat.S_ISDIR(info.st_mode) and not (stat.S_ISLNK(info.st_mode) and cursor.is_dir()):
            raise UnsafeTargetError(f"配置父级必须是实际目录：{cursor}")
        break
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode) and not (stat.S_ISLNK(info.st_mode) and directory.is_dir()):
                raise UnsafeTargetError(f"配置父级必须是实际目录：{directory}")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_temp_file(parent: Path, content: bytes) -> Path:
    fd, temp_name = tempfile.mkstemp(prefix=".workflow-", suffix=".tmp", dir=parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _create_new_file_atomically(path: Path, content: bytes) -> None:
    """Create a mode-0600 file without replacing anything already present."""

    _ensure_parent_directory(path.parent)
    temp_path = _write_temp_file(path.parent, content)
    try:
        os.link(temp_path, path)
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        raise ConcurrentModificationError(f"保存前目标路径已出现，未覆盖：{path}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def choose_backup_path(path: Path) -> Path:
    """Choose an unused sibling backup name without creating it."""

    first = Path(str(path) + ".bak")
    for number in range(0, 10_000):
        candidate = first if number == 0 else Path(str(first) + f".{number}")
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            return candidate
    raise OSError(f"找不到可用备份文件名：{first}")


def atomic_save(
    path: Path,
    content: bytes,
    expected: FileSnapshot,
    backup_path: Path | None = None,
) -> Path | None:
    """Save atomically after a fresh change check, preserving old bytes on overwrite."""

    path = _absolute(path)
    _ensure_parent_directory(path.parent)
    current = inspect_destination(path)
    if current != expected:
        raise ConcurrentModificationError(f"自检查以来目标已变化，未保存：{path}")

    if not expected.exists:
        _create_new_file_atomically(path, content)
        return None

    if expected.content is None:
        raise ConcurrentModificationError(f"无法验证现有目标内容：{path}")
    backup = _absolute(backup_path or choose_backup_path(path))
    _create_new_file_atomically(backup, expected.content)
    if inspect_destination(path) != expected:
        raise ConcurrentModificationError(f"创建备份期间目标已变化，未覆盖：{path}。旧内容备份保留在 {backup}")

    temp_path = _write_temp_file(path.parent, content)
    try:
        if inspect_destination(path) != expected:
            raise ConcurrentModificationError(f"保存期间目标已变化，未覆盖：{path}。旧内容备份保留在 {backup}")
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)
    return backup


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"workflow contains unsupported YAML value: {type(value).__name__}")


_SCENARIO_FIELD_ORDER = ("label", "permission_mode", "target", "fallbacks", "allow_fallback")


def _ordered_keys(d: dict[str, Any]) -> list[str]:
    if "label" in d and ("target" in d or "permission_mode" in d):
        ordered = [k for k in _SCENARIO_FIELD_ORDER if k in d]
        remaining = [k for k in d if k not in _SCENARIO_FIELD_ORDER]
        return ordered + remaining
    return list(d.keys())


def _yaml_lines(value: Any, indent: int = 0) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [pad + "{}"]
        lines: list[str] = []
        for key in _ordered_keys(value):
            child = value[key]
            key_text = _yaml_scalar(key) if isinstance(key, str) else _yaml_scalar(key)
            if isinstance(child, dict):
                if child:
                    lines.append(f"{pad}{key_text}:")
                    lines.extend(_yaml_lines(child, indent + 2))
                else:
                    lines.append(f"{pad}{key_text}: {{}}")
            elif isinstance(child, list):
                if child:
                    lines.append(f"{pad}{key_text}:")
                    lines.extend(_yaml_lines(child, indent + 2))
                else:
                    lines.append(f"{pad}{key_text}: []")
            else:
                lines.append(f"{pad}{key_text}: {_yaml_scalar(child)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [pad + "[]"]
        lines = []
        for child in value:
            if isinstance(child, dict):
                if child:
                    lines.append(pad + "-")
                    lines.extend(_yaml_lines(child, indent + 2))
                else:
                    lines.append(pad + "- {}")
            elif isinstance(child, list):
                if child:
                    lines.append(pad + "-")
                    lines.extend(_yaml_lines(child, indent + 2))
                else:
                    lines.append(pad + "- []")
            else:
                lines.append(f"{pad}- {_yaml_scalar(child)}")
        return lines
    return [pad + _yaml_scalar(value)]


def dump_yaml(workflow: dict[str, Any], unverified: Any = None) -> str:
    """Emit the workflow as safe block YAML using only JSON-quoted strings."""

    comments = [
        "# Generated by the Goals tmux workflow setup wizard.",
        "# Preferences only: the current runner does not automatically load or enforce this file.",
        "# This wizard does not request model generations or launch CLI sessions.",
        "# Model catalog checks may use a CLI's existing login; listed IDs do not prove quota or success.",
        "# This file contains no task-specific commit or permission-bypass authorization.",
    ]
    unverified_targets = set()
    direct_refs = set()
    if unverified:
        for item in unverified:
            if isinstance(item, (tuple, list)):
                target_str = item[0]
            elif isinstance(item, str):
                target_str = item.split("#")[0]
            else:
                continue
            if target_str.startswith("scenarios."):
                direct_refs.add(target_str)
            else:
                unverified_targets.add(target_str)

    unverified_bullets = list(direct_refs)
    for scenario_name, route in workflow.get("scenarios", {}).items():
        if not isinstance(route, dict):
            continue
        target = route.get("target")
        if target and target in unverified_targets:
            bullet = f"scenarios.{scenario_name}.target"
            if bullet not in unverified_bullets:
                unverified_bullets.append(bullet)
        fallbacks = route.get("fallbacks", [])
        if isinstance(fallbacks, list):
            for fb in fallbacks:
                if fb in unverified_targets:
                    bullet = f"scenarios.{scenario_name}.fallbacks"
                    if bullet not in unverified_bullets:
                        unverified_bullets.append(bullet)

    if unverified_bullets:
        comments.append("# Unverified selection(s); check them before any real launch:")
        for ref in sorted(unverified_bullets):
            comments.append(f"#   {ref}")

    bypass_scenarios = [
        name for name, route in workflow.get("scenarios", {}).items()
        if isinstance(route, dict) and route.get("permission_mode") == "bypass"
    ]
    if bypass_scenarios:
        comments.append("# permission_mode: bypass is a preference and does not grant current-task authorization.")
        comments.extend(f"#   scenarios.{name}.permission_mode" for name in sorted(bypass_scenarios))

    return "\n".join(comments + _yaml_lines(workflow)) + "\n"


def _default_discover(timeout: float) -> list[dict[str, Any]]:
    from .cli_discovery import discover_clis

    return discover_clis(timeout=timeout)


def _model_id(model: Any) -> str | None:
    if isinstance(model, str):
        return model
    if isinstance(model, dict):
        value = model.get("id", model.get("model_id"))
        return value if isinstance(value, str) and value else None
    return None


def _model_label(model: Any) -> str:
    identifier = _model_id(model) or "(无 ID)"
    if isinstance(model, dict):
        display = model.get("name") or model.get("display_name") or model.get("model_name")
        if isinstance(display, str) and display and display != identifier:
            return f"{identifier} — {display}"
    return identifier


def _cli_label(entry: dict[str, Any]) -> str:
    cli = str(entry.get("cli") or "unknown")
    return CLI_DISPLAY_NAMES.get(cli, cli)


def _unique_models(models: Any) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    if not isinstance(models, list):
        return result
    for model in models:
        identifier = _model_id(model)
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        result.append(model)
    return result


def _text_inventory(clis: list[dict[str, Any]]) -> str:
    lines = ["CLI 扫描结果（只读；不启动模型任务）："]
    if not clis:
        lines.append("  未返回受支持 CLI 的扫描结果。")
    for entry in clis:
        cli = str(entry.get("cli", "unknown"))
        installed = bool(entry.get("installed"))
        state = "已安装" if installed else "未安装"
        executable = entry.get("executable") or "不可用"
        version = entry.get("version") or "版本未知"
        models = _unique_models(entry.get("models"))
        model_state = str(entry.get("models_status") or "unknown")
        source = entry.get("models_source") or "无"
        lines.append(f"  {cli}: {state}; 路径={executable}; 版本={version}; 模型目录={model_state} ({len(models)} 个 ID); 来源={source}")
        for model in models[:8]:
            lines.append(f"    - {_model_label(model)}")
        if len(models) > 8:
            lines.append(f"    … 另有 {len(models) - 8} 个；使用 --scan --json 查看完整目录。")
        diagnostics = entry.get("diagnostics")
        if isinstance(diagnostics, list):
            for diagnostic in diagnostics:
                if isinstance(diagnostic, str) and diagnostic:
                    lines.append(f"    诊断：{diagnostic}")
    return "\n".join(lines)


def _interactive_inventory(clis: list[dict[str, Any]]) -> str:
    """Return the short inventory shown before interactive configuration."""

    installed = [entry for entry in clis if entry.get("installed") is True]
    lines = ["扫描完成。已安装 CLI："]
    if not installed:
        lines.append("  没有发现已安装的受支持 CLI。")
        lines.append("详细路径、版本和诊断请使用 --scan。")
        return "\n".join(lines)
    for entry in installed:
        models = _unique_models(entry.get("models"))
        if models:
            catalog = f"模型目录 {len(models)} 个 ID"
        else:
            catalog = "模型目录 0 个 ID"
        if str(entry.get("cli")) == "claude" and not models:
            action = "可选官方版本化模型建议或手动填写（本机目录/账号可用性未验证）"
        elif models:
            action = "可按编号选择，也可手动填写"
        else:
            action = "可手动填写未验证 ID"
        lines.append(f"  {_cli_label(entry)} — {catalog}；{action}")
    lines.append("详细路径、版本和诊断请使用 --scan。")
    return "\n".join(lines)


def _scan_json(clis: list[dict[str, Any]], timeout: float) -> str:
    return json.dumps(
        {"probe_timeout": timeout, "clis": clis},
        ensure_ascii=False,
        indent=2,
    )


class SetupWizard:
    """Prompt-driven workflow author with injectable scanner and streams."""

    def __init__(
        self,
        *,
        scanner: Callable[[float], list[dict[str, Any]]] | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        project_dir: Path | None = None,
        environ: dict[str, str] | None = None,
        home: Path | None = None,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    ) -> None:
        self.scanner = scanner or _default_discover
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.project_dir = Path(project_dir or Path.cwd()).expanduser().resolve()
        self.environ = dict(os.environ if environ is None else environ)
        self.home = Path.home() if home is None else Path(home)
        self.probe_timeout = probe_timeout
        self.workflow = new_workflow()
        self.clis: list[dict[str, Any]] = []
        self.working_pool: list[str] = []
        self.selected_clis: list[dict[str, Any]] = []
        self.unverified: list[tuple[str, str]] = []
        self.save_started = False
        self.save_destination: Path | None = None
        self.save_backup: Path | None = None
        self._edit_document: Any | None = None
        self._edit_snapshot: FileSnapshot | None = None
        self._edit_destination: Path | None = None
        self._edit_original_workflow: dict[str, Any] | None = None
        self._edit_scan_complete = False

    def write(self, text: str = "") -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def ask(self, prompt: str, default: str | None = None) -> str:
        suffix = f" [默认 {default}]" if default is not None else ""
        self.stdout.write(prompt + suffix + " ")
        self.stdout.flush()
        answer = self.stdin.readline()
        if answer == "":
            raise WizardCancelled("输入结束")
        answer = answer.rstrip("\r\n").strip()
        return default if answer == "" and default is not None else answer

    def ask_yes_no(self, prompt: str, default: bool = False) -> bool:
        default_text = "y" if default else "n"
        while True:
            answer = self.ask(f"{prompt} [y/n]", default_text).lower()
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.write("请输入 y 或 n。")

    def scan(self) -> list[dict[str, Any]]:
        result = self.scanner(self.probe_timeout)
        if not isinstance(result, list):
            raise ValueError("CLI 扫描器没有返回列表。")
        return [entry for entry in result if isinstance(entry, dict)]

    def installed_clis(self) -> list[dict[str, Any]]:
        return [entry for entry in self.clis if entry.get("installed") is True]

    def _target_summary(self, target: str) -> str:
        return target

    def choose_target_reference(self, prompt: str, excluded: set[str] | None = None) -> str | None:
        excluded = excluded or set()
        candidates = [t for t in self.working_pool if t not in excluded]
        if not candidates:
            return None
        for number, t in enumerate(candidates, 1):
            self.write(f"  {number}) {self._target_summary(t)}")
        while True:
            choice = self.ask(prompt)
            if choice.isdecimal() and 1 <= int(choice) <= len(candidates):
                return candidates[int(choice) - 1]
            self.write("请输入列表中的编号。")

    def _parse_number_list(self, answer: str, count: int, *, context: str) -> list[int] | None:
        """Parse an ordered, unique comma-separated list of one-based numbers.

        An empty list (blank or ``0``) is valid. Invalid lists are rejected as
        a whole so a malformed later item cannot leave a partial selection.
        """

        if answer in {"", "0"}:
            return []
        parts = [part.strip() for part in answer.split(",")]
        if any(not part.isdecimal() for part in parts):
            self.write(f"{context}格式无效；请输入逗号分隔的编号。")
            return None
        max_digits = max(1, len(str(count)))
        if any(len(part) > max_digits for part in parts):
            self.write(f"{context}包含超出范围的编号；请输入 1-{count}。")
            return None
        try:
            numbers = [int(part) for part in parts]
        except ValueError:
            self.write(f"{context}编号过长；请重新输入较短的编号。")
            return None
        if any(number == 0 for number in numbers):
            self.write("0/null 只能单独使用，不能与其他编号组合。")
            return None
        if any(number < 1 or number > count for number in numbers):
            self.write(f"{context}包含超出范围的编号；请输入 1-{count}。")
            return None
        if len(set(numbers)) != len(numbers):
            self.write(f"{context}包含重复编号；请每项只选择一次。")
            return None
        return numbers

    @staticmethod
    def _choice_number(answer: str, maximum: int) -> int | None:
        """Parse one bounded, one-based menu number without huge int conversion."""

        if not answer.isdecimal() or len(answer) > max(1, len(str(maximum))):
            return None
        try:
            number = int(answer)
        except ValueError:
            return None
        return number if 1 <= number <= maximum else None

    def _select_clis(self) -> list[dict[str, Any]]:
        available = self.installed_clis()
        for entry in self.clis:
            if entry.get("installed") is not True:
                self.write(f"  {entry.get('cli', 'unknown')}: 未安装；本向导不会安装或登录。")
        if not available:
            self.write("没有已安装的受支持 CLI；配置池为空，场景将保持未配置。")
            return []
        for number, entry in enumerate(available, 1):
            models = _unique_models(entry.get("models"))
            if models:
                catalog = f"模型目录 {len(models)} 个 ID"
            elif str(entry.get("cli")) == "claude":
                catalog = "模型列表未取得；可选官方版本化模型建议或手动填写"
            else:
                catalog = "模型列表未取得；可手动填写"
            self.write(
                f"  {number}) {_cli_label(entry)} — {catalog}"
            )
        while True:
            answer = self.ask("选择要加入配置池的 CLI 编号（逗号分隔；回车或 0 不选择）")
            numbers = self._parse_number_list(answer, len(available), context="CLI 列表")
            if numbers is not None:
                selected = [available[number - 1] for number in numbers]
                if selected:
                    self.write("已选择 CLI，顺序为：" + ", ".join(str(entry.get("cli")) for entry in selected))
                else:
                    self.write("未选择 CLI；将生成所有场景均未配置的模板。")
                return selected

    def _batch_effort_options(self, entry: dict[str, Any], model: Any) -> tuple[list[str], bool, str]:
        """Return effort choices, whether model support is verified, and their source."""

        if isinstance(model, dict):
            if model.get("_wizard_manual"):
                return list(QUICK_EFFORTS), False, "快速选项；未验证此手动模型支持"
            values = model.get("efforts")
            if isinstance(values, list):
                efforts = list(dict.fromkeys(value for value in values if isinstance(value, str) and value))
                if efforts:
                    if model.get("_wizard_claude_docs"):
                        return efforts, False, "Anthropic 官方文档列出；本机 CLI/账号支持未验证"
                    return efforts, True, "本机扫描报告的模型级选项"

        raw = entry.get("efforts")
        efforts = list(dict.fromkeys(value for value in raw if isinstance(value, str) and value)) if isinstance(raw, list) else []
        if efforts:
            scope = str(entry.get("efforts_scope") or "")
            status = str(entry.get("efforts_status") or "")
            verified = scope in {"model", "per_model", "model_specific"} and status in {"available", "ok", "discovered"}
            if verified:
                return efforts, True, "本机扫描报告的模型级选项"
            return efforts, False, "CLI 级提示；未确认此模型支持"
        return list(QUICK_EFFORTS), False, "快速选项；未验证此模型支持"

    def _print_model_row(self, number: int, model: Any, entry: dict[str, Any]) -> None:
        efforts, _verified, source = self._batch_effort_options(entry, model)
        effort_text = ", ".join(efforts)
        cli = str(entry.get("cli") or "unknown")
        self.write(f"  {number}) {_model_label(model)} | CLI={cli} | effort: null, {effort_text} | {source}")

    def _select_model_effort_batch(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """Compatibility wrapper for a one-CLI table and ordered batch."""

        selected = self._select_global_model_effort_batch([entry])
        return [{key: value for key, value in item.items() if key not in {"cli", "number"}} for item in selected]

    def _print_global_model_rows(self, rows: list[dict[str, Any]]) -> None:
        """Relist the current table without changing any global row numbers."""

        self.write("模型表完整重列（全局编号保持不变；不会重新扫描）：")
        if not rows:
            self.write("  （当前没有模型行。）")
        for row in rows:
            self._print_model_row(row["number"], row["model"], row["entry"])

    def _manual_model_cli(self, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Ask which selected CLI receives a manually entered model ID."""

        self.write("手动模型的目标 CLI：")
        for number, entry in enumerate(entries, 1):
            cli = str(entry.get("cli") or "unknown")
            self.write(f"  {number}) {_cli_label(entry)} (CLI={cli})")
        while True:
            answer = self.ask("选择目标 CLI 编号（回车或 b 返回；q 退出向导）").lower()
            if answer == "q":
                raise WizardCancelled("用户选择退出")
            if not answer or answer == "b":
                return None
            if answer.isdecimal() and 1 <= int(answer) <= len(entries):
                return entries[int(answer) - 1]
            self.write(f"请输入 1-{len(entries)}、b 或 q。")

    def _global_batch_example(self, rows: list[dict[str, Any]]) -> str:
        """Prefer one sample from each CLI group before repeating a group."""

        ordered: list[dict[str, Any]] = []
        seen_clis: set[str] = set()
        for row in rows:
            cli = str(row["entry"].get("cli") or "unknown")
            if cli not in seen_clis:
                ordered.append(row)
                seen_clis.add(cli)
        for row in rows:
            if row not in ordered:
                ordered.append(row)
            if len(ordered) >= 3:
                break
        examples: list[str] = []
        preferred = ("high", "xhigh", "max")
        for index, row in enumerate(ordered[:3]):
            options, _verified, _source = self._batch_effort_options(row["entry"], row["model"])
            effort = preferred[index] if preferred[index] in options else (options[0] if options else None)
            examples.append(str(row["number"]) if effort is None else f"{row['number']}-{effort}")
        return ", ".join(examples)

    def _parse_global_model_effort_batch(
        self,
        answer: str,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """Validate a cross-CLI batch atomically in the user's token order."""

        parts = answer.split(",")
        if any(not part.strip() for part in parts):
            self.write("组合格式无效；每项应为 全局编号、全局编号-effort 或 全局编号-null。")
            return None

        by_number = {int(row["number"]): row for row in rows}
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str | None]] = set()
        existing = set()
        for t in self.working_pool:
            seg = t.split("/")
            existing.add((seg[0], seg[1], seg[2] if len(seg) == 3 else None))
        for part in parts:
            match = re.fullmatch(r"(\d+)(?:\s*-\s*(.*))?", part.strip())
            if match is None:
                self.write("组合格式无效；每项应为 全局编号、全局编号-effort 或 全局编号-null。")
                return None
            try:
                number = int(match.group(1))
            except ValueError:
                self.write("全局模型编号格式无效；请输入列表中的编号。")
                return None
            row = by_number.get(number)
            if row is None:
                self.write(f"全局模型编号超出范围或不存在；请使用当前表中的编号（1-{len(rows)}）。")
                return None
            entry = row["entry"]
            model_record = row["model"]
            model = _model_id(model_record)
            cli = str(entry.get("cli") or "unknown")
            if model is None:
                self.write("所选行没有有效模型 ID；请重新选择。")
                return None

            raw_effort = match.group(2)
            if raw_effort is None or raw_effort.strip().casefold() == "null":
                effort = None
                effort_unverified = False
            else:
                effort = raw_effort.strip()
                options, verified, _source = self._batch_effort_options(entry, model_record)
                if not effort or effort not in options:
                    self.write(f"effort {effort!r} 不在全局编号 {number} 的选项中；可用值：null, {', '.join(options)}。")
                    return None
                effort_unverified = not verified

            identity = (cli, model, effort)
            if identity in seen or identity in existing:
                self.write(
                    f"包含重复组合：CLI={cli} / {model} / effort={effort if effort is not None else 'null'}；整行未添加。"
                )
                return None
            seen.add(identity)
            result.append({
                "cli": cli,
                "number": number,
                "model": model,
                "effort": effort,
                "model_unverified": bool(
                    isinstance(model_record, dict) and model_record.get("_wizard_model_unverified")
                ),
                "effort_unverified": effort_unverified,
            })
        return result

    def _select_global_model_effort_batch(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Print every selected CLI table, then collect one cross-CLI batch."""

        rows: list[dict[str, Any]] = []
        for entry in entries:
            cli = str(entry.get("cli") or "unknown")
            label = _cli_label(entry)
            models = _unique_models(entry.get("models"))
            from_claude_docs = not models and cli == "claude"
            if from_claude_docs:
                models = [
                    {
                        **record,
                        "_wizard_claude_docs": True,
                        "_wizard_model_unverified": True,
                    }
                    for record in CLAUDE_MODEL_FALLBACK
                ]

            start_number = len(rows) + 1
            if from_claude_docs:
                end = start_number + len(models) - 1
                self.write(
                    f"{label} (CLI={cli})：完整目录未取得；以下仅3个官方文档参考模型，并非全部模型。"
                    f"来源=Anthropic 官方文档；全局编号范围 {start_number}-{end}。"
                )
                self.write(f"  参考：{CLAUDE_MODEL_GUIDE}")
            elif models:
                source = entry.get("models_source") or "未提供"
                end = start_number + len(models) - 1
                self.write(
                    f"{label} (CLI={cli})：本次扫描返回 {len(models)} 项，全部列出；"
                    f"来源={source}；全局编号范围 {start_number}-{end}。"
                )
            else:
                source = entry.get("models_source") or "未提供"
                status = entry.get("models_status") or "unknown"
                self.write(
                    f"{label} (CLI={cli})：本次未取得模型列表；状态={status}；来源={source}；"
                    "详情见 --scan；无可选模型编号，可用 m 手动添加。"
                )

            for model in models:
                row = {"number": len(rows) + 1, "entry": entry, "model": model}
                rows.append(row)
                self._print_model_row(row["number"], model, entry)

        if not entries:
            self.write("没有选中 CLI；全局模型表为空，配置池保持为空。")
            return []
        self.write("所有已选 CLI 的模型表均已显示。")
        if rows:
            example = self._global_batch_example(rows)
            self.write(
                f"输入一个跨 CLI 全局模型-effort 批次（逗号分隔），例如 {example}；"
                "裸编号或 -null 表示不单独设置 effort。"
            )
        else:
            self.write("当前没有模型行；可用 m 添加手动模型，或用 s/空行跳过整个配置池。")
        self.write("m) 手动添加到所选 CLI 并追加全局编号；l) 重列全部行；s 或空行) 跳过整个配置池；q) 退出向导。")

        while True:
            answer = self.ask("跨 CLI 全局模型-effort 批次")
            normalized = answer.strip()
            command = normalized.casefold()
            if command == "q":
                raise WizardCancelled("用户选择退出")
            if not normalized or command == "s":
                return []
            if command == "l":
                self._print_global_model_rows(rows)
                continue
            if command == "m":
                target = self._manual_model_cli(entries)
                if target is None:
                    continue
                cli = str(target.get("cli") or "unknown")
                manual = self.ask(
                    f"输入 {cli} 的精确模型 ID（未验证；回车或 b 返回；q 退出向导）"
                )
                if manual.casefold() == "q":
                    raise WizardCancelled("用户选择退出")
                if not manual or manual.casefold() == "b":
                    continue
                if any(
                    row["entry"].get("cli") == target.get("cli") and _model_id(row["model"]) == manual
                    for row in rows
                ):
                    self.write("该 CLI 的精确 ID 已在全局表中；请使用现有编号。")
                    continue
                manual_record = {
                    "id": manual,
                    "_wizard_manual": True,
                    "_wizard_model_unverified": True,
                }
                row = {"number": len(rows) + 1, "entry": target, "model": manual_record}
                rows.append(row)
                self.write(f"已将手动模型追加到全局编号 {row['number']}；原有编号保持不变：")
                self._print_model_row(row["number"], manual_record, target)
                continue

            parsed = self._parse_global_model_effort_batch(answer, rows)
            if parsed is not None:
                return parsed

    def configure_pool(self) -> None:
        """Build the reusable CLI/model/effort target pool before scenarios."""

        self.write("\n=== 阶段 1/2：选择 CLI 并建立配置池 ===")
        self.selected_clis = self._select_clis()
        pending = self._select_global_model_effort_batch(self.selected_clis)
        if pending:
            self.write("已选组合（按输入顺序）：")
            for item in pending:
                effort = item["effort"] if item["effort"] is not None else "null"
                self.write(
                    f"  全局编号 {item['number']} → CLI={item['cli']} → ID={item['model']} → effort={effort}"
                )

            # Insert targets in the user's global batch order, even when CLIs interleave.
            for item in pending:
                cli = item["cli"]
                model = item["model"]
                effort = item["effort"]
                target_str = f"{cli}/{model}/{effort}" if effort is not None else f"{cli}/{model}"
                if target_str not in self.working_pool:
                    self.working_pool.append(target_str)
                if item["model_unverified"]:
                    self.unverified.append((target_str, "model"))
                if item["effort_unverified"]:
                    self.unverified.append((target_str, "effort"))
                self.write(f"已加入配置池：{self._target_summary(target_str)}。")
        elif self.selected_clis:
            self.write("已跳过整个配置池；所选 CLI 的组合均未更改。")
        if self.working_pool:
            self.write("配置池完成；场景阶段只能选择以下组合：")
            for number, target_str in enumerate(self.working_pool, 1):
                self.write(f"  {number}) {self._target_summary(target_str)}")
        else:
            self.write("配置池为空；可直接生成所有场景 target=null 的模板。")

    def _choose_primary(self, scenario: str, label: str) -> str | None:
        self.write(f"\n=== {label}（{scenario}）===")
        for number, target_str in enumerate(self.working_pool, 1):
            self.write(f"  {number}) {self._target_summary(target_str)}")
        self.write("  s) 跳过，保持 target: null")
        while True:
            answer = self.ask("选择配置池编号或 s 跳过", "s").lower()
            if answer.isdecimal() and 1 <= int(answer) <= len(self.working_pool):
                return self.working_pool[int(answer) - 1]
            if answer == "s":
                return None
            self.write("请输入配置池编号或 s。")

    def _add_fallbacks(self, scenario: str, primary: str) -> list[str]:
        candidates = list(self.working_pool)
        if len(candidates) < 2:
            return []
        while True:
            self.write("选择 fallback 配置池编号并用逗号排列优先级；回车、d 或 0 表示无 fallback。")
            for number, target_str in enumerate(candidates, 1):
                marker = "（primary，不能选作 fallback）" if target_str == primary else ""
                self.write(f"  {number}) {self._target_summary(target_str)}{marker}")
            answer = self.ask("fallback 编号（逗号分隔，按尝试顺序）", "d").lower()
            if answer in {"d", "done"}:
                return []
            numbers = self._parse_number_list(answer, len(candidates), context="fallback 列表")
            if numbers is None:
                continue
            if not numbers:
                return []
            selected = [candidates[number - 1] for number in numbers]
            if primary in selected:
                self.write("fallback 列表包含 primary；整组选择均未添加，请重新输入。")
                continue
            return selected

    def _ask_scenario_permission(self, scenario: str, label: str, current: str | None = None) -> str:
        """Ask one scenario-level permission_mode; current=None means creation flow (default 1)."""

        self.write(f"\n{label}（{scenario}）permission_mode（场景级，统一应用于 primary 和全部 fallback）：")
        self.write("  1) default（推荐）：沿用 CLI 当前权限设置")
        self.write("  2) bypass：请求 CLI 绕过权限确认（仍需当前任务明确授权）")
        if current is None:
            prompt, default = "permission_mode（1/2；兼容 d/b；q 退出向导）", "1"
        else:
            prompt, default = f"permission_mode：空行保留当前 {current}，1 default，2 bypass（q 取消）", None
        while True:
            answer = self.ask(prompt, default).lower()
            if answer == "q":
                raise WizardCancelled("用户选择退出" if current is None else "用户取消编辑")
            if answer == "" and current is not None:
                return current
            if answer in {"1", "d", "default"}:
                return "default"
            if answer in {"2", "b", "bypass"}:
                self.write("警告：YAML 中的 bypass 不能授权当前任务；执行时仍需当前任务明确授权。")
                return "bypass"
            self.write("请输入 1/default 或 2/bypass。" if current is None else "请输入空行、1/default 或 2/bypass。")

    def configure_scenarios(self) -> None:
        self.write("\n=== 阶段 2/2：为场景选择配置池组合 ===")
        if not self.working_pool:
            self.write("配置池为空；十个场景均保持 target: null，未配置 fallback。")
            for scenario, _ in SCENARIOS:
                route = self.workflow["scenarios"][scenario]
                route["target"] = None
                route["fallbacks"] = []
                route["allow_fallback"] = False
                route["permission_mode"] = "default"
            return
        for scenario, label in SCENARIOS:
            primary = self._choose_primary(scenario, label)
            route = self.workflow["scenarios"][scenario]
            if primary is None:
                route["target"] = None
                route["fallbacks"] = []
                route["allow_fallback"] = False
                route["permission_mode"] = "default"
                continue
            fallbacks = self._add_fallbacks(scenario, primary)
            route["target"] = primary
            route["fallbacks"] = fallbacks
            route["allow_fallback"] = bool(fallbacks)
            route["permission_mode"] = self._ask_scenario_permission(scenario, label)

    def configure_concurrency(self) -> None:
        used = set()
        for t in self.working_pool:
            cli = t.split("/")[0]
            if cli:
                used.add(cli)
        if not used:
            self.write("没有已选择的 CLI；并发默认值保持为每 CLI 4，无总量上限。")
            return
        overrides: dict[str, int] = self.workflow["concurrency"]["overrides"]
        self.write("设置每个已选 CLI 的并发上限；默认每 CLI 4 个，没有全局总量上限。")
        for cli in sorted(used):
            while True:
                answer = self.ask(f"{cli} 最大并发数（正整数）", "4")
                if answer.isdecimal() and int(answer) > 0:
                    maximum = int(answer)
                    if maximum != self.workflow["concurrency"]["default_max_per_cli"]:
                        overrides[cli] = maximum
                    break
                self.write("并发上限必须是正整数。")

    def _print_selection_summary(self) -> None:
        if self.unverified:
            self.write("未验证的手动输入、文档建议或模型支持不明的 effort（只作为偏好写入）：")
            for item in sorted(set(self.unverified)):
                if isinstance(item, (tuple, list)):
                    target_str, field = item
                elif "#" in item:
                    target_str, field = item.split("#", 1)
                else:
                    target_str, field = item, ""
                self.write(f"  {target_str} ({field})")
        bypass = [
            name for name, route in self.workflow.get("scenarios", {}).items()
            if isinstance(route, dict) and route.get("permission_mode") == "bypass"
        ]
        if bypass:
            self.write("permission_mode=bypass 是偏好，不授予当前任务权限：" + ", ".join(bypass))

    def choose_destination(self) -> Path:
        while True:
            try:
                user_path = str(user_config_destination(self.environ, self.home))
            except ValueError:
                user_path = "$XDG_CONFIG_HOME/goals-tmux-skill/workflow.yaml（XDG_CONFIG_HOME 必须是绝对路径）"

            try:
                project_path = str(project_destination(self.project_dir))
            except ValueError:
                project_path = str(_absolute(self.project_dir / ".goals-tmux.yaml"))

            try:
                base = _xdg_config_base(self.environ, self.home)
                host_template = str(_absolute(base / "goals-tmux-skill" / "hosts" / "<host-name>.yaml"))
            except ValueError:
                host_template = "$XDG_CONFIG_HOME/goals-tmux-skill/hosts/<host-name>.yaml（XDG_CONFIG_HOME 必须是绝对路径）"

            is_skill_dir = is_skill_installation_dir(self.project_dir)
            if is_skill_dir:
                self.write(
                    "\n警告：当前目录似乎是 skill 安装目录；保存在此处的项目配置在其他项目中不会被发现，且重新安装或升级 skill 时会被覆盖。"
                )

            self.write("\n配置层级说明（优先级低到高：user < host < project）：")
            self.write("  - user（用户层）：个人全局偏好")
            self.write("  - host（宿主层）：特定宿主客户端环境偏好")
            self.write("  - project（项目层）：当前项目专用偏好，优先级最高")
            self.write("选择保存位置：")
            self.write(f"  1) 用户配置 (user)：{user_path}")
            self.write(f"  2) 当前项目 (project)：{project_path}")
            self.write(f"  3) 宿主配置 (host)：{host_template}")
            answer = self.ask("输入 1/user、2/project 或 3/host", "2").lower()
            try:
                if answer in {"1", "u", "user", "1/user"}:
                    return user_config_destination(self.environ, self.home)
                if answer in {"2", "p", "project", "2/project"}:
                    if is_skill_dir:
                        self.write(
                            "警告：当前目录似乎是 skill 安装目录；保存在此处的项目配置在其他项目中不会被发现，且重新安装或升级 skill 时会被覆盖。"
                        )
                        if not self.ask_yes_no("确认保存到 skill 安装目录？", default=False):
                            continue
                    return project_destination(self.project_dir)
                if answer in {"3", "h", "host", "3/host"}:
                    while True:
                        raw_name = self.ask("输入 host 名称", "trae-cn")
                        try:
                            valid_name = validate_host_name(raw_name)
                            return host_config_destination(self.environ, self.home, valid_name)
                        except ValueError as exc:
                            self.write(str(exc))
                            continue
            except ValueError as exc:
                self.write(str(exc))
                continue
            self.write("请输入 1/user、2/project 或 3/host。")

    def choose_edit_destination(self) -> Path:
        """Ask which existing project/user file or custom path to edit."""

        while True:
            self.write("\n选择要编辑的现有工作流文件：")
            self.write(f"  1) 当前项目：{self.project_dir / '.goals-tmux.yaml'}")
            self.write("  2) 用户配置：$XDG_CONFIG_HOME/goals-tmux-skill/workflow.yaml（未设置时 ~/.config/...）")
            self.write("  3) 自定义路径")
            answer = self.ask("输入 1/project、2/user 或 3/custom（q 取消）", "1").lower()
            if answer == "q":
                raise WizardCancelled("用户取消编辑")
            try:
                if answer in {"1", "p", "project"}:
                    return _absolute(project_destination(self.project_dir))
                if answer in {"2", "u", "user"}:
                    return _absolute(user_config_destination(self.environ, self.home))
                if answer in {"3", "c", "custom"}:
                    custom = self.ask("输入现有工作流文件路径（q 取消）")
                    if custom.casefold() == "q":
                        raise WizardCancelled("用户取消编辑")
                    if custom:
                        return _absolute(Path(custom).expanduser())
                    self.write("自定义路径不能为空。")
                    continue
            except ValueError as exc:
                self.write(str(exc))
                continue
            self.write("请输入 1/project、2/user 或 3/custom。")

    def _load_edit_target(self, destination: Path) -> None:
        """Snapshot and parse one generated workflow before any prompts can edit it."""

        from .workflow_edit import parse_generated_yaml

        snapshot = inspect_destination(destination)
        if not snapshot.exists or snapshot.content is None:
            raise ValueError(f"编辑目标不存在或无法读取：{destination}")
        document = parse_generated_yaml(snapshot.content)
        workflow = copy.deepcopy(document.workflow)
        scenarios = workflow.get("scenarios")
        if not isinstance(scenarios, dict):
            raise ValueError("生成的工作流缺少可编辑的 scenarios。")

        self._edit_document = document
        self._edit_snapshot = snapshot
        self._edit_destination = destination
        self._edit_original_workflow = copy.deepcopy(workflow)
        self.workflow = workflow
        self.unverified = []

        # Seed working pool from existing scenarios (dedupe, preserve order)
        self.working_pool = []
        for route in self.workflow.get("scenarios", {}).values():
            if not isinstance(route, dict):
                continue
            target = route.get("target")
            if target and isinstance(target, str) and target not in self.working_pool:
                self.working_pool.append(target)
            for fb in route.get("fallbacks", []):
                if fb and isinstance(fb, str) and fb not in self.working_pool:
                    self.working_pool.append(fb)

    def _edit_scenario_updates(self) -> dict[str, dict[str, Any]]:
        original_scenarios = (self._edit_original_workflow or {}).get("scenarios", {})
        updates: dict[str, dict[str, Any]] = {}
        for name, route in self.workflow.get("scenarios", {}).items():
            original = original_scenarios.get(name)
            if not isinstance(route, dict) or not isinstance(original, dict):
                continue
            changed = {
                key: copy.deepcopy(route[key])
                for key in ("permission_mode", "target", "fallbacks", "allow_fallback")
                if key in route and route.get(key) != original.get(key)
            }
            if changed:
                updates[name] = changed
        return updates

    def _edit_bytes_and_diff(self) -> tuple[bytes, str]:
        from .workflow_edit import patch_generated_yaml

        if self._edit_document is None or self._edit_snapshot is None or self._edit_destination is None:
            raise RuntimeError("编辑目标尚未加载。")
        updates = self._edit_scenario_updates()
        output = patch_generated_yaml(self._edit_document, updates)
        original = self._edit_snapshot.content
        if original is None:
            raise RuntimeError("编辑目标快照缺少原始内容。")
        if output == original:
            return output, ""
        old_lines = original.decode("utf-8").splitlines(keepends=True)
        new_lines = output.decode("utf-8").splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=str(self._edit_destination),
            tofile=f"{self._edit_destination} (修改后)",
        ))
        return output, diff

    def _show_edit_diff(self) -> tuple[bytes, str]:
        output, diff = self._edit_bytes_and_diff()
        self.write("\n========== 工作流差异 ==========")
        if diff:
            self.write(diff.rstrip("\n"))
        else:
            self.write("未修改。")
        self.write("========== 差异结束 ==========")
        return output, diff

    def _edit_add_targets(self) -> None:
        if not self._edit_scan_complete:
            self.write("新增模型需要一次只读 CLI/模型目录扫描；场景编辑无需扫描。")
            self.clis = self.scan()
            self._edit_scan_complete = True
            self.write(_interactive_inventory(self.clis))
        self.configure_pool()

    def _edit_choose_scenario(self) -> str | None:
        scenarios = self.workflow.get("scenarios", {})
        choices = list(scenarios.items())
        if not choices:
            self.write("此工作流没有可编辑的场景。")
            return None
        self.write("场景列表（按文件中保存的标签显示）：")
        for number, (name, route) in enumerate(choices, 1):
            label = route.get("label") if isinstance(route, dict) else None
            self.write(f"  {number}) {label if isinstance(label, str) and label else name} ({name})")
        self.write("  0) 返回编辑菜单")
        while True:
            answer = self.ask("选择要修改的场景（编号；q 取消编辑）").lower()
            if answer == "q":
                raise WizardCancelled("用户取消编辑")
            if answer == "0":
                return None
            number = self._choice_number(answer, len(choices))
            if number is not None:
                return choices[number - 1][0]
            self.write(f"请输入 1-{len(choices)}、0 或 q。")

    def _edit_scenario_route(self, scenario: str) -> None:
        route = self.workflow["scenarios"][scenario]
        if not isinstance(route, dict):
            self.write(f"场景 {scenario} 的配置格式不可编辑。")
            return
        label = route.get("label") if isinstance(route.get("label"), str) else scenario
        current_target = route.get("target")
        current_fallbacks = route.get("fallbacks", [])
        current_allow = route.get("allow_fallback", False)
        current_perm = route.get("permission_mode", "default")
        self.write(f"\n{label} ({scenario}) 当前配置：")
        self.write(f"  primary: {current_target if current_target is not None else 'null'}")
        self.write(f"  fallbacks: {', '.join(current_fallbacks) if current_fallbacks else '无'}")
        self.write(f"  allow_fallback: {current_allow}")
        self.write(f"  permission_mode: {current_perm}")
        self.write("可选配置池组合：")
        if self.working_pool:
            for number, target_str in enumerate(self.working_pool, 1):
                self.write(f"  {number}) {self._target_summary(target_str)}")
        else:
            self.write("  （配置池为空；可清除 primary，或保留现状。）")

        primary_cleared = False
        while True:
            answer = self.ask("primary：空行保留，0 清除，或输入配置池编号（q 取消）").lower()
            if answer == "q":
                raise WizardCancelled("用户取消编辑")
            if answer == "":
                primary = current_target
                break
            if answer == "0":
                primary = None
                primary_cleared = True
                break
            number = self._choice_number(answer, len(self.working_pool))
            if number is not None:
                primary = self.working_pool[number - 1]
                break
            self.write(f"请输入空行、0 或 1-{len(self.working_pool)}。")

        if primary_cleared:
            fallbacks = []
            allow_fallback = False
            perm_mode = "default"
            self.write("primary 已清除；该场景的 fallbacks 和 allow_fallback 也会清除。")
        else:
            while True:
                answer = self.ask("fallback：空行保留现有顺序，0 清除，或输入有序编号（逗号分隔；q 取消）").lower()
                if answer == "q":
                    raise WizardCancelled("用户取消编辑")
                if answer == "":
                    fallbacks = list(current_fallbacks) if isinstance(current_fallbacks, list) else []
                    allow_fallback = current_allow if isinstance(current_allow, bool) else bool(fallbacks)
                elif answer == "0":
                    fallbacks = []
                    allow_fallback = False
                else:
                    numbers = self._parse_number_list(answer, len(self.working_pool), context="fallback 列表")
                    if numbers is None:
                        continue
                    fallbacks = [self.working_pool[number - 1] for number in numbers]
                    allow_fallback = bool(fallbacks)
                    if primary is None and fallbacks:
                        self.write("该场景没有 primary；请先设置 primary，或输入 0 清除 fallbacks。")
                        continue
                if primary in fallbacks:
                    self.write("fallback 列表包含 primary；请改选 fallbacks 或输入 0 清除。")
                    continue
                break

            if primary is not None:
                perm_mode = self._ask_scenario_permission(
                    scenario, label, current_perm if current_perm in {"default", "bypass"} else "default"
                )
            else:
                perm_mode = "default"

        route["target"] = primary
        route["fallbacks"] = fallbacks
        route["allow_fallback"] = allow_fallback
        route["permission_mode"] = perm_mode
        self.write(f"已更新内存中的 {scenario} 场景；写入会等到保存确认后进行。")

    def _edit_save(self) -> int:
        output, diff = self._show_edit_diff()
        if not diff:
            self.write("未修改；没有创建备份或写入文件。")
            return 0
        if self._edit_destination is None or self._edit_snapshot is None:
            raise RuntimeError("编辑目标尚未加载。")
        backup = choose_backup_path(self._edit_destination)
        self.write(f"\n精确编辑路径：{self._edit_destination}")
        self.write(f"旧文件备份路径：{backup}")
        if not self.ask_yes_no("按以上差异保存并备份旧文件？", default=False):
            self.write("已取消编辑；没有创建备份或写入文件。")
            return 0

        self.save_started = True
        self.save_destination = self._edit_destination
        self.save_backup = backup
        try:
            saved_backup = atomic_save(
                self._edit_destination,
                output,
                self._edit_snapshot,
                backup,
            )
        except (OSError, UnsafeTargetError, ConcurrentModificationError) as exc:
            self.write(f"保存失败：{exc}")
            if backup.exists():
                self.write(f"旧文件备份仍保留在：{backup}")
            return 1
        self.write(f"已保存：{self._edit_destination}")
        if saved_backup is not None:
            self.write(f"旧文件备份：{saved_backup}")
        self.write("此文件仅记录偏好；当前 runner 不会自动加载或强制执行其中配置。")
        return 0

    def run_edit(self, destination: Path | None = None) -> int:
        """Edit a previously generated workflow while preserving untouched bytes."""

        self.write("Goals tmux YAML 编辑向导")
        self.write("只编辑生成的偏好文件；当前 runner 不会自动加载或执行它。场景编辑不会扫描 CLI。")
        if destination is not None:
            target = _absolute(Path(destination).expanduser())
            self._load_edit_target(target)
        else:
            while True:
                target = self.choose_edit_destination()
                try:
                    self._load_edit_target(target)
                    break
                except (OSError, ValueError, UnsafeTargetError, ConcurrentModificationError) as exc:
                    self.write(f"无法编辑此目标：{exc}")

        self.write(f"编辑目标：{self._edit_destination}")
        while True:
            self.write("\n编辑菜单：")
            self.write("  1) 新增模型/effort 组合（按需扫描；只添加新组合）")
            self.write("  2) 修改指定场景")
            self.write("  3) 查看差异")
            self.write("  4) 保存退出")
            self.write("  q) 取消")
            choice = self.ask("选择操作").lower()
            if choice in {"q", "quit", "cancel"}:
                raise WizardCancelled("用户取消编辑")
            if choice == "1":
                self._edit_add_targets()
                continue
            if choice == "2":
                scenario = self._edit_choose_scenario()
                if scenario is not None:
                    self._edit_scenario_route(scenario)
                continue
            if choice == "3":
                self._show_edit_diff()
                continue
            if choice == "4":
                return self._edit_save()
            self.write("请输入 1、2、3、4 或 q。")

    def run(self) -> int:
        self.write("Goals tmux YAML 配置向导")
        self.write("只生成偏好文件：当前 runner 不会自动加载或执行它。不会启动 CLI 任务或发起模型生成请求。")
        self.write("模型目录探测可能通过现有 CLI 登录访问服务；扫描不会安装 CLI 或代替登录。")
        self.clis = self.scan()
        self.write(_interactive_inventory(self.clis))
        self.configure_pool()
        self.configure_scenarios()
        self.configure_concurrency()
        self.write("\nTUI 设置保留模板默认值：keep_open=true，allow_input=true。")
        self._print_selection_summary()

        yaml_text = dump_yaml(self.workflow, self.unverified)
        self.write("\n========== YAML 预览 ==========")
        self.write(yaml_text.rstrip("\n"))
        self.write("========== 预览结束 ==========")
        destination = self.choose_destination()
        while True:
            try:
                snapshot = inspect_destination(destination)
                break
            except (OSError, UnsafeTargetError, ConcurrentModificationError) as exc:
                self.write(f"无法使用此目标：{exc}")
                destination = self.choose_destination()
        self.write(f"\n精确保存路径：{destination}")
        backup: Path | None = None
        if snapshot.exists:
            backup = choose_backup_path(destination)
            self.write(f"目标文件已存在，将完整备份为：{backup}")
            self.write("向导不会解析或合并已有 YAML。确认覆盖会用本次完整工作流替换目标。")
        if not self.ask_yes_no("保存本次生成的工作流？", default=False):
            self.write("已取消；没有写入配置文件。")
            return 0
        if snapshot.exists and not self.ask_yes_no("单独确认：覆盖已有文件并保留上述备份？", default=False):
            self.write("已取消覆盖；没有写入配置文件。")
            return 0
        self.save_started = True
        self.save_destination = destination
        self.save_backup = backup
        try:
            backup = atomic_save(destination, yaml_text.encode("utf-8"), snapshot, backup)
        except (OSError, UnsafeTargetError, ConcurrentModificationError) as exc:
            self.write(f"保存失败：{exc}")
            if backup is not None and backup.exists():
                self.write(f"已有文件的备份仍保留在：{backup}")
            return 1
        if backup is not None:
            self.write(f"已保存；旧文件备份：{backup}")
        else:
            self.write(f"已保存：{destination}")
        self.write("此文件仅记录偏好；当前 runner 不会自动加载或强制执行其中配置。")
        return 0


def _probe_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("probe timeout must be a number") from exc
    if not MIN_PROBE_TIMEOUT <= timeout <= MAX_PROBE_TIMEOUT:
        raise argparse.ArgumentTypeError(
            f"probe timeout must be between {MIN_PROBE_TIMEOUT:g} and {MAX_PROBE_TIMEOUT:g} seconds"
        )
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "First shortlist installed CLIs and model/effort targets, then assign scenarios from that pool; "
            "fallback targets accept comma-separated priority order. Generate a proposed workflow YAML only; "
            "the current runner does not automatically load or enforce it.\n"
            "Model batch example: 1-high, 3-xhigh, 5-max; a bare model number or 1-null omits effort."
        )
    )
    parser.add_argument("--scan", action="store_true", help="scan CLIs and exit without prompts or writes")
    parser.add_argument("--json", action="store_true", help="emit the read-only inventory as JSON and exit")
    parser.add_argument(
        "--edit",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="edit a previously generated workflow (choose project/user/custom path when PATH is omitted)",
    )
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help="project directory for .goals-tmux.yaml")
    parser.add_argument(
        "--probe-timeout",
        type=_probe_timeout,
        default=DEFAULT_PROBE_TIMEOUT,
        help=f"bounded timeout per discovery probe ({MIN_PROBE_TIMEOUT:g}..{MAX_PROBE_TIMEOUT:g}s; default {DEFAULT_PROBE_TIMEOUT:g})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.edit is not None and (args.scan or args.json):
        parser.error("--edit cannot be combined with --scan or --json")
    if not args.project_dir.expanduser().exists() or not args.project_dir.expanduser().is_dir():
        parser.error(f"project directory does not exist or is not a directory: {args.project_dir}")
    wizard = SetupWizard(project_dir=args.project_dir, probe_timeout=args.probe_timeout)
    try:
        if args.edit is not None:
            destination = Path(args.edit) if args.edit else None
            return wizard.run_edit(destination)
        if args.scan or args.json:
            clis = wizard.scan()
            if args.json:
                print(_scan_json(clis, args.probe_timeout))
            else:
                print(_text_inventory(clis))
            return 0
        return wizard.run()
    except WizardCancelled as exc:
        print(f"已取消（{exc}）；没有写入配置文件。", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        if wizard.save_started and wizard.save_destination is not None:
            details = f"保存阶段收到中断；请检查目标文件状态：{wizard.save_destination}"
            if wizard.save_backup is not None:
                details += f"；检查备份：{wizard.save_backup}"
            print(details, file=sys.stderr)
        else:
            print("已取消；没有写入配置文件。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"向导失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
