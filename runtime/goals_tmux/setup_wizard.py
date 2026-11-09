"""Interactive generator for a proposed Goals tmux workflow YAML file.

The wizard records user preferences only. The current runner does not load or
enforce the resulting YAML, and this module never launches a model session.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, Callable, TextIO

from .workflow_template import new_workflow


DEFAULT_PROBE_TIMEOUT = 8.0
MIN_PROBE_TIMEOUT = 0.1
MAX_PROBE_TIMEOUT = 30.0
MAX_EXISTING_CONFIG_BYTES = 1024 * 1024
MODEL_PAGE_SIZE = 20

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


def user_config_destination(environ: dict[str, str] | None = None, home: Path | None = None) -> Path:
    """Return the XDG user workflow path, requiring an absolute XDG base."""

    environment = os.environ if environ is None else environ
    configured = environment.get("XDG_CONFIG_HOME")
    if configured:
        base = Path(configured)
        if not base.is_absolute():
            raise ValueError("XDG_CONFIG_HOME 必须是绝对路径；请改正环境变量或选择项目配置。")
    else:
        base = (Path.home() if home is None else Path(home)) / ".config"
    return _absolute(base / "goals-tmux-skill" / "workflow.yaml")


def project_destination(project_dir: Path) -> Path:
    """Return the workflow path in the selected project directory."""

    root = Path(project_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"项目目录不存在或不是目录：{root}")
    return root / ".goals-tmux.yaml"


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


def _yaml_lines(value: Any, indent: int = 0) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [pad + "{}"]
        lines: list[str] = []
        for key, child in value.items():
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


def dump_yaml(workflow: dict[str, Any], unverified: list[tuple[str, str]] | None = None) -> str:
    """Emit the workflow as safe block YAML using only JSON-quoted strings."""

    comments = [
        "# Generated by the Goals tmux workflow setup wizard.",
        "# Preferences only: the current runner does not automatically load or enforce this file.",
        "# This wizard does not request model generations or launch CLI sessions.",
        "# Model catalog checks may use a CLI's existing login; listed IDs do not prove quota or success.",
        "# This file contains no task-specific commit or permission-bypass authorization.",
    ]
    manual_fields = sorted(set(unverified or []))
    if manual_fields:
        comments.append("# Unverified selection(s); check them before any real launch:")
        comments.extend(f"#   profiles.{profile}.{field}" for profile, field in manual_fields)
    bypass_profiles = [
        name for name, profile in workflow.get("profiles", {}).items()
        if isinstance(profile, dict) and profile.get("permission_mode") == "bypass"
    ]
    if bypass_profiles:
        comments.append("# permission_mode: bypass is a preference and does not grant current-task authorization.")
        comments.extend(f"#   profiles.{name}.permission_mode" for name in bypass_profiles)
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
        self.profiles: dict[str, dict[str, Any]] = self.workflow["profiles"]
        self.unverified: list[tuple[str, str]] = []
        self.save_started = False
        self.save_destination: Path | None = None
        self.save_backup: Path | None = None

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

    def _profile_summary(self, name: str) -> str:
        profile = self.profiles[name]
        model = profile.get("model")
        effort = profile.get("effort")
        text = f"{name} — {profile.get('cli')} / {model} / effort={effort if effort is not None else 'null'}"
        if profile.get("permission_mode") == "bypass":
            text += " / permission_mode=bypass"
        return text

    def choose_profile_reference(self, prompt: str, excluded: set[str] | None = None) -> str | None:
        excluded = excluded or set()
        candidates = [name for name in self.profiles if name not in excluded]
        if not candidates:
            return None
        for number, name in enumerate(candidates, 1):
            self.write(f"  {number}) {self._profile_summary(name)}")
        while True:
            choice = self.ask(prompt)
            if choice.isdecimal() and 1 <= int(choice) <= len(candidates):
                return candidates[int(choice) - 1]
            self.write("请输入列表中的编号。")

    def _select_cli(self) -> dict[str, Any] | None:
        available = self.installed_clis()
        for entry in self.clis:
            if entry.get("installed") is not True:
                self.write(f"  {entry.get('cli', 'unknown')}: 未安装；本向导不会安装或登录。")
        if not available:
            self.write("没有已安装的受支持 CLI；此场景会保持未配置。")
            return None
        for number, entry in enumerate(available, 1):
            self.write(
                f"  {number}) {entry.get('cli')} — {entry.get('executable') or '路径未知'}"
                f" — {entry.get('version') or '版本未知'}"
            )
        while True:
            answer = self.ask("选择已安装的 CLI 编号")
            if answer.isdecimal() and 1 <= int(answer) <= len(available):
                return available[int(answer) - 1]
            self.write("请输入列表中的编号。")

    def _select_model(self, entry: dict[str, Any]) -> tuple[str, Any | None, bool]:
        models = _unique_models(entry.get("models"))
        status = str(entry.get("models_status") or "unknown")
        source = entry.get("models_source") or "无"
        self.write(f"模型目录状态：{status}；来源：{source}。目录 ID 不代表额度或可成功运行。")
        if not models:
            self.write("没有可核对的模型 ID。请显式手动填写；本向导不会猜测默认模型。")
            while True:
                model = self.ask("输入精确模型 ID（手动值未验证）")
                if model:
                    return model, None, True
                self.write("模型 ID 不能为空。")

        self.write(f"发现 {len(models)} 个模型 ID。显示精确 ID；不会合并或改写名称。")
        visible = list(models)
        page = 0
        query: str | None = None
        while True:
            start = page * MODEL_PAGE_SIZE
            end = min(start + MODEL_PAGE_SIZE, len(visible))
            self.write(f"模型 {start + 1}-{end}/{len(visible)}" + (f"，筛选={query}" if query is not None else ""))
            for index in range(start, end):
                self.write(f"  {index - start + 1}) {_model_label(visible[index])}")
            self.write("输入编号选择；n/p 翻页；/关键词筛选；* 显示全部；=精确ID；m 手动填写未验证 ID。")
            answer = self.ask("模型选择").strip()
            if answer.isdecimal() and 1 <= int(answer) <= end - start:
                model = visible[start + int(answer) - 1]
                return str(_model_id(model)), model, False
            if answer == "n":
                if end < len(visible):
                    page += 1
                else:
                    self.write("已经是最后一页。")
                continue
            if answer == "p":
                if page > 0:
                    page -= 1
                else:
                    self.write("已经是第一页。")
                continue
            if answer.startswith("/") and len(answer) > 1:
                needle = answer[1:].casefold()
                visible = [
                    model for model in models
                    if needle in _model_label(model).casefold()
                ]
                query = answer[1:]
                page = 0
                if not visible:
                    self.write("没有匹配项。输入 * 恢复完整列表，或 m 手动填写。")
                continue
            if answer == "*":
                visible = list(models)
                query = None
                page = 0
                continue
            if answer.startswith("="):
                exact = answer[1:]
                for model in models:
                    if _model_id(model) == exact:
                        return exact, model, False
                self.write("该 ID 不在已发现目录中。如需使用，请输入 m 后手动填写，并按未验证值处理。")
                continue
            if answer.lower() == "m":
                while True:
                    model = self.ask("输入精确模型 ID（手动值未验证）")
                    if model:
                        return model, None, True
                    self.write("模型 ID 不能为空。")
            self.write("无法识别选择。")

    def _effort_values(self, entry: dict[str, Any], model: Any | None) -> tuple[list[str], bool]:
        if isinstance(model, dict):
            per_model = model.get("efforts")
            if isinstance(per_model, list):
                values = [value for value in per_model if isinstance(value, str) and value]
                if values:
                    return list(dict.fromkeys(values)), True
        raw = entry.get("efforts")
        values = [value for value in raw if isinstance(value, str) and value] if isinstance(raw, list) else []
        if not values:
            return [], False
        scope = str(entry.get("efforts_scope") or "")
        status = str(entry.get("efforts_status") or "")
        model_verified = scope in {"model", "per_model", "model_specific"} and status in {"available", "ok", "discovered"}
        return list(dict.fromkeys(values)), model_verified

    def _select_effort(self, entry: dict[str, Any], model: Any | None, profile_name: str) -> str | None:
        efforts, verified = self._effort_values(entry, model)
        status = str(entry.get("efforts_status") or "unknown")
        if efforts:
            self.write("可见 effort 参数：")
            for number, effort in enumerate(efforts, 1):
                self.write(f"  {number}) {effort}")
            if verified:
                self.write(f"  0) 不设置独立 effort（null；目录对当前模型提供了值）")
            else:
                self.write(f"  0) 不设置独立 effort（null）")
                self.write(f"这些是 CLI 级参数提示（状态={status}），没有证据确认适用于刚选的模型。")
            self.write("  m) 手动填写（未验证）")
            while True:
                answer = self.ask("选择 effort 编号、0/null 或 m", "0").lower()
                if answer in {"0", "null", "none"}:
                    return None
                if answer.isdecimal() and 1 <= int(answer) <= len(efforts):
                    if not verified:
                        self.unverified.append((profile_name, "effort"))
                    return efforts[int(answer) - 1]
                if answer == "m":
                    while True:
                        effort = self.ask("输入精确 effort 值（未验证）")
                        if effort:
                            self.unverified.append((profile_name, "effort"))
                            return effort
                        self.write("effort 不能为空；使用 0/null 可不设置。")
                self.write("请输入有效编号、0 或 m。")

        self.write(f"没有发现 effort 值（状态={status}）。可手动填写未验证值，或留空生成 null。")
        while True:
            answer = self.ask("选择 m 手动填写，或 0 不设置", "0").lower()
            if answer in {"0", "null", "none"}:
                return None
            if answer == "m":
                while True:
                    effort = self.ask("输入精确 effort 值（未验证）")
                    if effort:
                        self.unverified.append((profile_name, "effort"))
                        return effort
                    self.write("effort 不能为空；使用 0/null 可不设置。")
            self.write("请输入 0 或 m。")

    def _new_profile_name(self, scenario: str, cli: str, kind: str) -> str:
        stem = f"{scenario}_{cli}" if kind == "primary" else f"{scenario}_{kind}_{cli}"
        name = stem
        number = 2
        while name in self.profiles:
            name = f"{stem}_{number}"
            number += 1
        return name

    def configure_profile(self, scenario: str, kind: str = "primary") -> str | None:
        entry = self._select_cli()
        if entry is None:
            return None
        cli = str(entry.get("cli"))
        name = self._new_profile_name(scenario, cli, kind)
        model, model_record, model_manual = self._select_model(entry)
        if model_manual:
            self.unverified.append((name, "model"))
        effort = self._select_effort(entry, model_record, name)
        self.write("权限模式 default 不会请求额外 bypass；bypass 仍须有本任务单独授权。")
        while True:
            permission = self.ask("permission_mode: d=default, b=bypass（仅偏好，不是授权）", "d").lower()
            if permission in {"d", "default"}:
                permission_mode = "default"
                break
            if permission in {"b", "bypass"}:
                permission_mode = "bypass"
                self.write("警告：YAML 中的 bypass 不能授权当前任务；执行时仍需当前任务明确授权。")
                break
            self.write("请输入 d 或 b。")
        self.profiles[name] = {
            "cli": cli,
            "model": model,
            "effort": effort,
            "permission_mode": permission_mode,
        }
        self.write(f"已生成配置 {name}。")
        return name

    def _choose_primary(self, scenario: str, label: str) -> str | None:
        self.write(f"\n=== {label}（{scenario}）===")
        if self.profiles:
            for number, name in enumerate(self.profiles, 1):
                self.write(f"  {number}) 复用 {self._profile_summary(name)}")
            self.write("  n) 新建 CLI/model/effort 配置")
            self.write("  s) 跳过，保持 profile: null")
            default = "s"
            while True:
                answer = self.ask("选择配置编号、n 新建或 s 跳过", default).lower()
                names = list(self.profiles)
                if answer.isdecimal() and 1 <= int(answer) <= len(names):
                    return names[int(answer) - 1]
                if answer == "n":
                    return self.configure_profile(scenario, "primary")
                if answer == "s":
                    return None
                self.write("请输入列表编号、n 或 s。")
        self.write("输入 n 新建配置，或 s 跳过（该场景保持 profile: null）。")
        while True:
            answer = self.ask("选择 n 新建或 s 跳过", "s").lower()
            if answer == "n":
                return self.configure_profile(scenario, "primary")
            if answer == "s":
                return None
            self.write("请输入 n 或 s。")

    def _add_fallbacks(self, scenario: str, primary: str) -> list[str]:
        if scenario == "review":
            self.write("Review 场景固定不配置 fallback。")
            return []
        fallbacks: list[str] = []
        while True:
            self.write("可按顺序添加 fallback profile；回车完成（默认无 fallback）。")
            for number, name in enumerate(self.profiles, 1):
                marker = "（已选择）" if name in fallbacks else ""
                self.write(f"  {number}) {self._profile_summary(name)}{marker}")
            if self.installed_clis():
                self.write("  n) 新建一个 fallback profile")
            self.write("  d) 完成 fallback 列表")
            answer = self.ask("选择 profile 编号、n 新建或 d 完成", "d").lower()
            if answer in {"d", ""}:
                return fallbacks
            if answer.isdecimal() and 1 <= int(answer) <= len(self.profiles):
                name = list(self.profiles)[int(answer) - 1]
                if name == primary:
                    self.write("primary profile 不能同时作为 fallback。")
                elif name in fallbacks:
                    self.write("该 fallback 已在列表中。")
                else:
                    fallbacks.append(name)
                    self.write(f"已添加第 {len(fallbacks)} 顺位 fallback：{name}")
                continue
            if answer == "n" and self.installed_clis():
                name = self.configure_profile(scenario, f"fallback{len(fallbacks) + 1}")
                if name is not None:
                    fallbacks.append(name)
                continue
            self.write("请输入列表编号、n 或 d。")

    def configure_scenarios(self) -> None:
        for scenario, label in SCENARIOS:
            primary = self._choose_primary(scenario, label)
            route = self.workflow["scenarios"][scenario]
            route["profile"] = primary
            if primary is None:
                route["fallbacks"] = []
                route["allow_fallback"] = False
                continue
            fallbacks = self._add_fallbacks(scenario, primary)
            route["fallbacks"] = fallbacks
            route["allow_fallback"] = bool(fallbacks) and scenario != "review"

    def configure_concurrency(self) -> None:
        used = {profile.get("cli") for profile in self.profiles.values() if profile.get("cli")}
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
            self.write("未验证的手动或 CLI 级选择（只作为偏好写入）：")
            for profile_name, field in sorted(set(self.unverified)):
                value = self.profiles[profile_name].get(field)
                self.write(f"  profiles.{profile_name}.{field} = {json.dumps(value, ensure_ascii=False)}")
        bypass = [
            name for name, profile in self.profiles.items()
            if profile.get("permission_mode") == "bypass"
        ]
        if bypass:
            self.write("permission_mode=bypass 是偏好，不授予当前任务权限：" + ", ".join(bypass))

    def choose_destination(self) -> Path:
        while True:
            self.write("\n选择保存位置：")
            self.write("  1) 用户配置：$XDG_CONFIG_HOME/goals-tmux-skill/workflow.yaml（未设置时 ~/.config/...）")
            self.write(f"  2) 当前项目：{self.project_dir / '.goals-tmux.yaml'}")
            answer = self.ask("输入 1/user 或 2/project", "2").lower()
            try:
                if answer in {"1", "u", "user"}:
                    return user_config_destination(self.environ, self.home)
                if answer in {"2", "p", "project"}:
                    return project_destination(self.project_dir)
            except ValueError as exc:
                self.write(str(exc))
                continue
            self.write("请输入 1 或 2。")

    def run(self) -> int:
        self.write("Goals tmux YAML 配置向导")
        self.write("只生成偏好文件：当前 runner 不会自动加载或执行它。不会启动 CLI 任务或发起模型生成请求。")
        self.write("模型目录探测可能通过现有 CLI 登录访问服务；扫描不会安装 CLI 或代替登录。")
        self.clis = self.scan()
        self.write(_text_inventory(self.clis))
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
        description="Scan supported CLI model catalogs and interactively generate a proposed workflow YAML."
    )
    parser.add_argument("--scan", action="store_true", help="scan CLIs and exit without prompts or writes")
    parser.add_argument("--json", action="store_true", help="emit the read-only inventory as JSON and exit")
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
    if not args.project_dir.expanduser().exists() or not args.project_dir.expanduser().is_dir():
        parser.error(f"project directory does not exist or is not a directory: {args.project_dir}")
    wizard = SetupWizard(project_dir=args.project_dir, probe_timeout=args.probe_timeout)
    try:
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
