"""Normalize terminal pane events into per-pane summaries."""

import math
import re

_PANE_ID_RE = re.compile(r"%[0-9]+")
_KINDS = ("output", "exit")


def _is_valid_timestamp(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        # Python ints are always finite; math.isfinite would overflow on huge ones.
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def _validate(index, record):
    if not isinstance(record, dict):
        raise ValueError(f"event {index}: expected dict, got {type(record).__name__}")

    pane_id = record.get("pane_id")
    if not isinstance(pane_id, str) or not _PANE_ID_RE.fullmatch(pane_id):
        raise ValueError(f"event {index}: invalid pane_id {pane_id!r}")

    kind = record.get("kind")
    if kind not in _KINDS:
        raise ValueError(f"event {index}: invalid kind {kind!r}")

    timestamp = record.get("timestamp")
    if not _is_valid_timestamp(timestamp):
        raise ValueError(f"event {index}: invalid timestamp {timestamp!r}")

    if kind == "output":
        if not isinstance(record.get("text"), str):
            raise ValueError(f"event {index}: output requires str text")
    else:
        status = record.get("status")
        if not isinstance(status, int) or isinstance(status, bool):
            raise ValueError(f"event {index}: exit requires int status")


def normalize_events(events):
    """Return {pane_id: {"output", "exit_status", "last_timestamp"}}.

    Raises ValueError on any invalid record. The input is never mutated.
    """
    chunks = {}
    summary = {}
    for index, record in enumerate(events):
        _validate(index, record)
        pane_id = record["pane_id"]
        timestamp = record["timestamp"]
        entry = summary.get(pane_id)
        if entry is None:
            entry = {"output": "", "exit_status": None, "last_timestamp": timestamp}
            summary[pane_id] = entry
            chunks[pane_id] = []
        elif timestamp > entry["last_timestamp"]:
            entry["last_timestamp"] = timestamp

        if record["kind"] == "output":
            chunks[pane_id].append(record["text"])
        else:
            entry["exit_status"] = record["status"]

    for pane_id, entry in summary.items():
        entry["output"] = "".join(chunks[pane_id])
    return summary
