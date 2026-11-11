"""Bounded parser and source-preserving editor for wizard-generated workflows.

This intentionally supports the small YAML subset emitted by ``setup_wizard``.
It is not a general YAML loader. Strings and mapping keys use JSON quoting,
collections use block indentation, and the only flow values are ``{}``/``[]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
from typing import Any


MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_LINES = 50_000
MAX_NODES = 20_000
MAX_DEPTH = 32

_PLAIN_KEY = re.compile(r"[A-Za-z0-9_.-]+\Z")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_SCENARIO_FIELDS = frozenset({"profile", "fallbacks", "allow_fallback"})


class WorkflowEditError(ValueError):
    """Raised when a document or requested edit is outside the supported format."""


@dataclass
class _Node:
    kind: str
    value: Any
    start_line: int
    end_line: int
    entries: dict[str, "_Entry"] = field(default_factory=dict)


@dataclass
class _Entry:
    key: str
    key_token: str
    line: int
    node: _Node

    @property
    def end_line(self) -> int:
        return self.node.end_line


@dataclass(frozen=True)
class EditDocument:
    """Parsed workflow plus original bytes and private source-map data."""

    workflow: dict[str, Any]
    raw: bytes
    _root: _Node = field(repr=False, compare=False)
    _lines: tuple[str, ...] = field(repr=False, compare=False)
    _line_starts: tuple[int, ...] = field(repr=False, compare=False)
    _line_ends: tuple[int, ...] = field(repr=False, compare=False)
    _newline: bytes = field(repr=False, compare=False)


@dataclass
class _Token:
    line: int
    indent: int
    text: str


class _Parser:
    def __init__(self, raw: bytes) -> None:
        if not isinstance(raw, bytes):
            raise TypeError("workflow document must be bytes")
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise WorkflowEditError(f"workflow document exceeds {MAX_DOCUMENT_BYTES} bytes")
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowEditError(f"workflow document is not valid UTF-8 at byte {exc.start}") from exc

        byte_lines = raw.splitlines(keepends=True)
        if len(byte_lines) > MAX_LINES:
            raise WorkflowEditError(f"workflow document exceeds {MAX_LINES} lines")
        self.lines: list[str] = []
        self.line_starts: list[int] = []
        self.line_ends: list[int] = []
        offset = 0
        for line_bytes in byte_lines:
            self.line_starts.append(offset)
            offset += len(line_bytes)
            self.line_ends.append(offset)
            line = line_bytes.decode("utf-8")
            if line.endswith("\n"):
                line = line[:-1]
                if line.endswith("\r"):
                    line = line[:-1]
            elif line.endswith("\r"):
                line = line[:-1]
            self.lines.append(line)
        # bytes.splitlines() returns no row for b""; keep the empty source map.
        if not byte_lines and decoded:
            self.lines = [decoded]
            self.line_starts = [0]
            self.line_ends = [len(raw)]

        self.tokens: list[_Token] = []
        for index, line in enumerate(self.lines):
            if "\t" in line[: len(line) - len(line.lstrip(" \t"))]:
                self.fail("tab indentation is unsupported", index)
            indent = len(line) - len(line.lstrip(" "))
            text = line[indent:]
            if not text or text.startswith("#"):
                continue
            if text == "---" or text.startswith("--- ") or text == "..." or text.startswith("... "):
                self.fail("multiple YAML documents and document markers are unsupported", index)
            self.tokens.append(_Token(index, indent, text))
        self.position = 0
        self.node_count = 0

    def fail(self, message: str, line: int | None = None) -> None:
        if line is None:
            raise WorkflowEditError(message)
        raise WorkflowEditError(f"line {line + 1}: {message}")

    def _count_node(self, line: int) -> None:
        self.node_count += 1
        if self.node_count > MAX_NODES:
            self.fail(f"workflow exceeds {MAX_NODES} parsed nodes", line)

    @staticmethod
    def _is_sequence(text: str) -> bool:
        return text == "-" or text.startswith("- ")

    def parse(self) -> _Node:
        if not self.tokens:
            raise WorkflowEditError("workflow document is empty")
        if self.tokens[0].indent != 0:
            self.fail("root value must start at column 1", self.tokens[0].line)
        if self._is_sequence(self.tokens[0].text):
            self.fail("workflow root must be a mapping", self.tokens[0].line)
        root = self._parse_block(0, 0)
        if self.position != len(self.tokens):
            token = self.tokens[self.position]
            self.fail("unexpected indentation or unsupported YAML syntax", token.line)
        if root.kind != "map":
            self.fail("workflow root must be a mapping", root.start_line)
        return root

    def _parse_block(self, indent: int, depth: int) -> _Node:
        if depth > MAX_DEPTH:
            token = self.tokens[self.position]
            self.fail(f"workflow nesting exceeds {MAX_DEPTH} levels", token.line)
        token = self.tokens[self.position]
        if token.indent != indent:
            self.fail("invalid indentation; generated YAML uses two-space levels", token.line)
        if self._is_sequence(token.text):
            return self._parse_sequence(indent, depth)
        return self._parse_mapping(indent, depth)

    def _parse_mapping(self, indent: int, depth: int) -> _Node:
        first_line = self.tokens[self.position].line
        mapping: dict[str, Any] = {}
        entries: dict[str, _Entry] = {}
        last_end = first_line + 1
        while self.position < len(self.tokens):
            token = self.tokens[self.position]
            if token.indent < indent:
                break
            if token.indent > indent:
                self.fail("unexpected indentation; generated YAML uses two-space levels", token.line)
            if self._is_sequence(token.text):
                break

            key, key_token, rest = self._parse_key_value(token)
            if key in entries:
                self.fail(f"duplicate mapping key {key!r}", token.line)
            self.position += 1
            if rest:
                node = self._parse_inline_value(rest, token.line)
            else:
                if self.position >= len(self.tokens) or self.tokens[self.position].indent <= indent:
                    self.fail("empty block values are unsupported; use null, {}, or []", token.line)
                child = self.tokens[self.position]
                if child.indent != indent + 2:
                    self.fail("invalid indentation; nested values must be indented by two spaces", child.line)
                node = self._parse_block(indent + 2, depth + 1)
            mapping[key] = node.value
            entries[key] = _Entry(key, key_token, token.line, node)
            last_end = node.end_line
        self._count_node(first_line)
        return _Node("map", mapping, first_line, last_end, entries)

    def _parse_sequence(self, indent: int, depth: int) -> _Node:
        first_line = self.tokens[self.position].line
        items: list[Any] = []
        last_end = first_line + 1
        while self.position < len(self.tokens):
            token = self.tokens[self.position]
            if token.indent < indent:
                break
            if token.indent > indent:
                self.fail("unexpected indentation in sequence", token.line)
            if not self._is_sequence(token.text):
                break
            rest = self._without_inline_comment(token.text[1:].strip(), token.line).strip()
            self.position += 1
            if rest:
                node = self._parse_inline_value(rest, token.line)
            else:
                if self.position >= len(self.tokens) or self.tokens[self.position].indent <= indent:
                    self.fail("empty sequence items are unsupported", token.line)
                child = self.tokens[self.position]
                if child.indent != indent + 2:
                    self.fail("invalid indentation; nested sequence items use two-space levels", child.line)
                node = self._parse_block(indent + 2, depth + 1)
            items.append(node.value)
            last_end = node.end_line
        self._count_node(first_line)
        return _Node("list", items, first_line, last_end)

    def _parse_key_value(self, token: _Token) -> tuple[str, str, str]:
        text = token.text
        if text.startswith('"'):
            key, end = self._decode_json_string(text, token.line)
            if not isinstance(key, str):
                self.fail("mapping keys must be strings", token.line)
            tail = text[end:]
            if not tail.startswith(":"):
                self.fail("mapping entries must use a string key followed by a colon", token.line)
            key_token = text[:end]
            rest = tail[1:]
        else:
            colon = text.find(":")
            if colon <= 0:
                self.fail("mapping entries must use a JSON-quoted or safe plain key", token.line)
            key_token = text[:colon]
            if not _PLAIN_KEY.fullmatch(key_token):
                self.fail("plain mapping keys must contain only letters, digits, dot, underscore, or hyphen", token.line)
            rest = text[colon + 1 :]
            key = key_token
        rest = self._without_inline_comment(rest, token.line).strip()
        if rest and not text.startswith('"'):
            # No whitespace is allowed inside the plain key, but spaces after
            # the colon are normal YAML formatting.
            pass
        return key, key_token, rest

    def _decode_json_string(self, text: str, line: int) -> tuple[str, int]:
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except (json.JSONDecodeError, ValueError) as exc:
            self.fail("invalid JSON-quoted string", line)
        if not isinstance(value, str):
            self.fail("only JSON-quoted strings may be used as strings", line)
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            self.fail("surrogate code points are unsupported", line)
        return value, end

    @staticmethod
    def _without_inline_comment(text: str, line: int) -> str:
        quoted = False
        escaped = False
        for index, char in enumerate(text):
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "#" and (index == 0 or text[index - 1].isspace()):
                return text[:index].rstrip()
        return text

    def _parse_inline_value(self, text: str, line: int) -> _Node:
        if text == "{}":
            self._count_node(line)
            return _Node("map", {}, line, line + 1)
        if text == "[]":
            self._count_node(line)
            return _Node("list", [], line, line + 1)
        if text == "null":
            value: Any = None
        elif text == "true":
            value = True
        elif text == "false":
            value = False
        elif text.startswith('"'):
            value, end = self._decode_json_string(text, line)
            if end != len(text):
                self.fail("extra text after JSON-quoted string", line)
        elif _NUMBER.fullmatch(text):
            if len(text) > 128:
                self.fail("numeric scalar is too long", line)
            try:
                value = json.loads(text)
            except (ValueError, json.JSONDecodeError) as exc:
                self.fail("invalid numeric scalar", line)
            if isinstance(value, float) and not math.isfinite(value):
                self.fail("non-finite numeric values are unsupported", line)
        else:
            if text.startswith(("&", "*", "!", "|", ">")):
                self.fail("anchors, aliases, tags, and multiline scalars are unsupported", line)
            if any(char in text for char in "{}[]"):
                self.fail("flow collections are unsupported except exact {} and []", line)
            self.fail("unsupported plain scalar; generated string values must be JSON-quoted", line)
        self._count_node(line)
        return _Node("scalar", value, line, line + 1)


def parse_generated_yaml(raw: bytes) -> EditDocument:
    """Parse the wizard's bounded YAML subset and retain exact source spans."""

    parser = _Parser(raw)
    root = parser.parse()
    workflow = root.value
    _validate_workflow(workflow)
    newline = b"\r\n" if b"\r\n" in raw else b"\n"
    return EditDocument(
        workflow=workflow,
        raw=raw,
        _root=root,
        _lines=tuple(parser.lines),
        _line_starts=tuple(parser.line_starts),
        _line_ends=tuple(parser.line_ends),
        _newline=newline,
    )


def patch_generated_yaml(
    document: EditDocument,
    profiles_to_add: dict[str, dict[str, Any]],
    scenario_updates: dict[str, dict[str, Any]],
    unverified: list[tuple[str, str]] = (),
) -> bytes:
    """Patch only requested profile and scenario source spans.

    The input document is never mutated. Unsupported or invalid edits raise
    ``WorkflowEditError`` before returning any bytes to be written.
    """

    if not isinstance(document, EditDocument):
        raise TypeError("document must be an EditDocument")
    if not isinstance(profiles_to_add, dict) or not isinstance(scenario_updates, dict):
        raise WorkflowEditError("profile and scenario edits must be mappings")
    if not isinstance(unverified, (list, tuple)):
        raise WorkflowEditError("unverified selections must be a list of (profile, field) pairs")

    existing_profiles = document.workflow["profiles"]
    new_profiles: dict[str, dict[str, Any]] = {}
    for name, profile in profiles_to_add.items():
        if not isinstance(name, str) or not name:
            raise WorkflowEditError("new profile names must be nonempty strings")
        if name in existing_profiles:
            raise WorkflowEditError(f"profile {name!r} already exists")
        if not isinstance(profile, dict):
            raise WorkflowEditError(f"new profile {name!r} must be a mapping")
        _validate_renderable(profile)
        new_profiles[name] = profile

    updates: dict[str, dict[str, Any]] = {}
    for scenario, changes in scenario_updates.items():
        if not isinstance(scenario, str) or scenario not in document.workflow["scenarios"]:
            raise WorkflowEditError(f"unknown scenario {scenario!r}")
        if not isinstance(changes, dict):
            raise WorkflowEditError(f"scenario update for {scenario!r} must be a mapping")
        if any(not isinstance(key, str) for key in changes):
            raise WorkflowEditError(f"scenario update keys for {scenario!r} must be strings")
        unknown = set(changes) - _SCENARIO_FIELDS
        if unknown:
            raise WorkflowEditError(f"unsupported scenario fields for {scenario!r}: {sorted(unknown)!r}")
        _validate_renderable(changes)
        updates[scenario] = changes

    _validate_renderable({"profiles": new_profiles, "scenarios": updates})

    unverified_items: list[tuple[str, str]] = []
    for item in unverified:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise WorkflowEditError("unverified selections must be (profile, field) pairs")
        profile_name, field_name = item
        if not isinstance(profile_name, str) or not isinstance(field_name, str):
            raise WorkflowEditError("unverified profile and field names must be strings")
        if profile_name not in new_profiles:
            raise WorkflowEditError(f"unverified field must belong to a newly added profile: {profile_name!r}")
        if field_name not in new_profiles[profile_name]:
            raise WorkflowEditError(f"unverified field {profile_name}.{field_name} does not exist")
        unverified_items.append((profile_name, field_name))

    final_workflow = _deepcopy_workflow(document.workflow)
    final_workflow["profiles"].update(new_profiles)
    for scenario, changes in updates.items():
        final_workflow["scenarios"][scenario].update(changes)
    _validate_workflow(final_workflow)

    operations: list[tuple[int, int, bytes]] = []
    line_starts = document._line_starts
    line_ends = document._line_ends
    newline_text = document._newline.decode("ascii")

    profiles_entry = document._root.entries["profiles"]
    profiles_node = profiles_entry.node
    if new_profiles:
        if profiles_node.kind != "map":
            raise WorkflowEditError("profiles must be a mapping")
        rendered_profiles = [
            *_render_block(new_profiles, indent=2).splitlines(),
        ]
        if not profiles_node.entries:
            entry_line = profiles_entry.line
            line = document._lines[entry_line]
            colon = _mapping_colon(line.lstrip(" "))
            if colon < 0:
                raise WorkflowEditError("profiles mapping source span is invalid")
            prefix_end = len(line) - len(line.lstrip(" ")) + colon + 1
            prefix = line[:prefix_end].rstrip()
            replacement_text = prefix + newline_text + newline_text.join(rendered_profiles)
            old_start = line_starts[entry_line]
            old_end = line_ends[entry_line]
            had_newline = old_end > old_start and document.raw[old_end - 1 : old_end] in (b"\n", b"\r")
            trivia = _source_trivia(document, entry_line, entry_line + 1)
            if had_newline or trivia:
                replacement_text += newline_text
            if trivia:
                replacement_text += trivia.decode("utf-8")
            elif had_newline:
                replacement_text += newline_text
            operations.append((old_start, old_end, replacement_text.encode("utf-8")))
        else:
            insert_line = profiles_node.end_line - 1
            offset = line_ends[insert_line]
            at_eof = offset == len(document.raw)
            had_final_newline = bool(document.raw.endswith((b"\n", b"\r")))
            prefix = b"" if offset == 0 or document.raw[offset - 1 : offset] in (b"\n", b"\r") else document._newline
            insertion = prefix + (newline_text.join(rendered_profiles)).encode("utf-8")
            if not at_eof or had_final_newline:
                insertion += document._newline
            operations.append((offset, offset, insertion))

    scenarios_entry = document._root.entries["scenarios"]
    scenarios_node = scenarios_entry.node
    for scenario, changes in updates.items():
        route_entry = scenarios_node.entries.get(scenario)
        if route_entry is None or route_entry.node.kind != "map":
            raise WorkflowEditError(f"scenario {scenario!r} has no editable mapping source span")
        route_node = route_entry.node
        missing: list[tuple[str, Any]] = []
        for field_name, value in changes.items():
            current_route = document.workflow["scenarios"][scenario]
            if field_name in current_route and current_route[field_name] == value:
                continue
            field_entry = route_node.entries.get(field_name)
            if field_entry is None:
                missing.append((field_name, value))
                continue
            first = field_entry.line
            last = field_entry.end_line - 1
            old_start = line_starts[first]
            old_end = line_ends[last]
            replacement_lines = _render_entry(field_name, value, indent=4)
            replacement_text = newline_text.join(replacement_lines)
            had_newline = old_end > old_start and document.raw[old_end - 1 : old_end] in (b"\n", b"\r")
            trivia = _source_trivia(document, first, field_entry.end_line)
            if had_newline or trivia:
                replacement_text += newline_text
            if trivia:
                replacement_text += trivia.decode("utf-8")
            elif had_newline:
                replacement_text += newline_text
            operations.append((old_start, old_end, replacement_text.encode("utf-8")))
        if missing:
            rendered = [line for field_name, value in missing for line in _render_entry(field_name, value, indent=4)]
            insert_line = route_node.end_line - 1
            offset = line_ends[insert_line]
            at_eof = offset == len(document.raw)
            had_final_newline = bool(document.raw.endswith((b"\n", b"\r")))
            prefix = b"" if offset == 0 or document.raw[offset - 1 : offset] in (b"\n", b"\r") else document._newline
            insertion = prefix + newline_text.join(rendered).encode("utf-8")
            if not at_eof or had_final_newline:
                insertion += document._newline
            operations.append((offset, offset, insertion))

    bypass_profiles = [name for name, profile in new_profiles.items() if profile.get("permission_mode") == "bypass"]
    warning_lines = _new_warning_lines(document, unverified_items, bypass_profiles)
    if warning_lines:
        root_start = document._root.start_line
        offset = line_starts[root_start]
        warning_text = newline_text.join(warning_lines) + newline_text
        operations.append((offset, offset, warning_text.encode("utf-8")))

    if not operations:
        return document.raw
    operations.sort(key=lambda op: (op[0], op[1]), reverse=True)
    output = document.raw
    previous_start = len(output) + 1
    for start, end, replacement in operations:
        if end > previous_start:
            raise WorkflowEditError("overlapping source edits are not supported")
        output = output[:start] + replacement + output[end:]
        previous_start = start
    if len(output) > MAX_DOCUMENT_BYTES:
        raise WorkflowEditError(f"patched workflow exceeds {MAX_DOCUMENT_BYTES} bytes")
    # Parse and validate the candidate before returning it to the caller.
    parse_generated_yaml(output)
    return output


def _mapping_colon(text: str) -> int:
    if text.startswith('"'):
        try:
            _value, end = json.JSONDecoder().raw_decode(text)
            return end if end < len(text) and text[end] == ":" else -1
        except (json.JSONDecodeError, ValueError):
            return -1
    return text.find(":")


def _render_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    raise WorkflowEditError(f"unsupported value type {type(value).__name__}")


def _render_entry(key: str, value: Any, indent: int) -> list[str]:
    pad = " " * indent
    key_text = json.dumps(key, ensure_ascii=False)
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{key_text}: {{}}"]
        return [f"{pad}{key_text}:", *_render_block(value, indent + 2).splitlines()]
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key_text}: []"]
        rows: list[str] = [f"{pad}{key_text}:"]
        rows.extend(_render_sequence(value, indent + 2).splitlines())
        return rows
    return [f"{pad}{key_text}: {_render_scalar(value)}"]


def _render_block(value: dict[str, Any] | list[Any], indent: int) -> str:
    if isinstance(value, dict):
        lines: list[str] = []
        for key, child in value.items():
            lines.extend(_render_entry(key, child, indent))
        return "\n".join(lines)
    return _render_sequence(value, indent)


def _render_sequence(values: list[Any], indent: int) -> str:
    pad = " " * indent
    lines: list[str] = []
    for value in values:
        if isinstance(value, dict):
            if value:
                lines.append(pad + "-")
                lines.extend(_render_block(value, indent + 2).splitlines())
            else:
                lines.append(pad + "- {}")
        elif isinstance(value, list):
            if value:
                lines.append(pad + "-")
                lines.extend(_render_sequence(value, indent + 2).splitlines())
            else:
                lines.append(pad + "- []")
        else:
            lines.append(f"{pad}- {_render_scalar(value)}")
    return "\n".join(lines)


def _validate_renderable(value: Any) -> None:
    active: set[int] = set()
    count = 0
    estimated_text_bytes = 0

    def account_text(text: str) -> None:
        nonlocal estimated_text_bytes
        # JSON escaping can expand a control character to six ASCII bytes.
        estimated_text_bytes += len(text) * 6
        if estimated_text_bytes > MAX_DOCUMENT_BYTES:
            raise WorkflowEditError("requested edit text exceeds the document size limit")

    def visit(item: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if count > MAX_NODES:
            raise WorkflowEditError(f"requested edit exceeds {MAX_NODES} values")
        if depth > MAX_DEPTH:
            raise WorkflowEditError(f"requested edit exceeds {MAX_DEPTH} nesting levels")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise WorkflowEditError("surrogate code points are unsupported")
            account_text(item)
            return
        if isinstance(item, int):
            if item.bit_length() > 426:
                raise WorkflowEditError("numeric scalar is too long")
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise WorkflowEditError("non-finite numeric values are unsupported")
            return
        if isinstance(item, (list, dict)):
            identity = id(item)
            if identity in active:
                raise WorkflowEditError("cyclic values are unsupported")
            active.add(identity)
            if isinstance(item, list):
                children = item
            else:
                if any(not isinstance(key, str) for key in item):
                    raise WorkflowEditError("mapping keys in requested edits must be strings")
                for key in item:
                    account_text(key)
                children = item.values()
            for child in children:
                visit(child, depth + 1)
            active.remove(identity)
            return
        raise WorkflowEditError(f"unsupported value type {type(item).__name__}")

    visit(value, 0)


def _deepcopy_workflow(value: dict[str, Any]) -> dict[str, Any]:
    # Parsed values are trees, so the tiny serializer-supported structures can
    # be copied recursively without importing a general YAML dependency.
    if isinstance(value, dict):
        return {key: _deepcopy_workflow(child) if isinstance(child, dict) else _deepcopy_list(child) for key, child in value.items()}
    raise WorkflowEditError("workflow root must be a mapping")


def _deepcopy_list(value: Any) -> Any:
    if isinstance(value, dict):
        return _deepcopy_workflow(value)
    if isinstance(value, list):
        return [_deepcopy_list(child) for child in value]
    return value


def _validate_workflow(workflow: Any) -> None:
    if not isinstance(workflow, dict):
        raise WorkflowEditError("workflow root must be a mapping")
    if workflow.get("kind") != "goals-tmux-workflow":
        raise WorkflowEditError("workflow.kind must be 'goals-tmux-workflow'")
    version = workflow.get("schema_version")
    if type(version) is not int or version != 1:
        raise WorkflowEditError("workflow.schema_version must be integer 1")
    profiles = workflow.get("profiles")
    scenarios = workflow.get("scenarios")
    if not isinstance(profiles, dict):
        raise WorkflowEditError("workflow.profiles must be a mapping")
    if not isinstance(scenarios, dict):
        raise WorkflowEditError("workflow.scenarios must be a mapping")
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name:
            raise WorkflowEditError("profile names must be nonempty strings")
        if not isinstance(profile, dict):
            raise WorkflowEditError(f"profiles.{name} must be a mapping")
        if not isinstance(profile.get("cli"), str) or not profile["cli"].strip():
            raise WorkflowEditError(f"profiles.{name}.cli must be a nonempty string")
        if not isinstance(profile.get("model"), str) or not profile["model"].strip():
            raise WorkflowEditError(f"profiles.{name}.model must be a nonempty string")
        if "effort" not in profile or (profile["effort"] is not None and not isinstance(profile["effort"], str)):
            raise WorkflowEditError(f"profiles.{name}.effort must be a string or null")
        if profile.get("permission_mode") not in ("default", "bypass"):
            raise WorkflowEditError(f"profiles.{name}.permission_mode must be 'default' or 'bypass'")

    for scenario, route in scenarios.items():
        if not isinstance(scenario, str) or not scenario:
            raise WorkflowEditError("scenario names must be nonempty strings")
        if not isinstance(route, dict):
            raise WorkflowEditError(f"scenarios.{scenario} must be a mapping")
        if "profile" not in route or "fallbacks" not in route or "allow_fallback" not in route:
            raise WorkflowEditError(f"scenarios.{scenario} must define profile, fallbacks, and allow_fallback")
        primary = route["profile"]
        if primary is not None and (not isinstance(primary, str) or primary not in profiles):
            raise WorkflowEditError(f"scenarios.{scenario}.profile must name an existing profile or be null")
        fallbacks = route["fallbacks"]
        if not isinstance(fallbacks, list) or any(not isinstance(item, str) or item not in profiles for item in fallbacks):
            raise WorkflowEditError(f"scenarios.{scenario}.fallbacks must list existing profile names")
        if len(set(fallbacks)) != len(fallbacks):
            raise WorkflowEditError(f"scenarios.{scenario}.fallbacks must not contain duplicates")
        if primary is not None and primary in fallbacks:
            raise WorkflowEditError(f"scenarios.{scenario}.fallbacks must not repeat the primary profile")
        if primary is None and fallbacks:
            raise WorkflowEditError(f"scenarios.{scenario}.fallbacks require a primary profile")
        allowed = route["allow_fallback"]
        if not isinstance(allowed, bool) or allowed != bool(fallbacks):
            raise WorkflowEditError(f"scenarios.{scenario}.allow_fallback must match whether fallbacks are configured")


def _source_trivia(document: EditDocument, first_line: int, end_line: int) -> bytes:
    """Retain comments and blank lines inside a replaced source span."""

    preserved: list[bytes] = []
    for index in range(first_line, end_line):
        line = document._lines[index]
        start = document._line_starts[index]
        end = document._line_ends[index]
        raw_line = document.raw[start:end]
        if not line.strip() or line.lstrip().startswith("#"):
            preserved.append(raw_line)
            continue
        comment = _line_comment_start(line)
        if comment is None:
            continue
        indent = line[: len(line) - len(line.lstrip(" "))]
        if raw_line.endswith(b"\r\n"):
            ending = b"\r\n"
        elif raw_line.endswith(b"\n"):
            ending = b"\n"
        elif raw_line.endswith(b"\r"):
            ending = b"\r"
        else:
            ending = b""
        preserved.append((indent + line[comment:]).encode("utf-8") + ending)
    return b"".join(preserved)


def _line_comment_start(line: str) -> int | None:
    quoted = False
    escaped = False
    for index, char in enumerate(line):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return index
    return None


def _new_warning_lines(
    document: EditDocument,
    unverified: list[tuple[str, str]],
    bypass_profiles: list[str],
) -> list[str]:
    if not unverified and not bypass_profiles:
        return []
    existing = {line.strip() for line in document._lines if line.lstrip().startswith("#")}
    lines: list[str] = []

    unverified_heading = "# Unverified selection(s); check them before any real launch:"
    unverified_bullets: list[str] = []
    for profile, field_name in sorted(set(unverified)):
        safe_profile = profile.replace("\r", "\\r").replace("\n", "\\n")
        safe_field = field_name.replace("\r", "\\r").replace("\n", "\\n")
        bullet = f"#   profiles.{safe_profile}.{safe_field}"
        if bullet not in existing and bullet not in unverified_bullets:
            unverified_bullets.append(bullet)
    if unverified_bullets:
        if unverified_heading not in existing:
            lines.append(unverified_heading)
        lines.extend(unverified_bullets)

    bypass_heading = "# permission_mode: bypass is a preference and does not grant current-task authorization."
    bypass_bullets: list[str] = []
    for profile in bypass_profiles:
        safe_profile = profile.replace("\r", "\\r").replace("\n", "\\n")
        bullet = f"#   profiles.{safe_profile}.permission_mode"
        if bullet not in existing and bullet not in bypass_bullets:
            bypass_bullets.append(bullet)
    if bypass_bullets:
        if bypass_heading not in existing:
            lines.append(bypass_heading)
        lines.extend(bypass_bullets)
    return lines


__all__ = ["EditDocument", "WorkflowEditError", "parse_generated_yaml", "patch_generated_yaml"]
