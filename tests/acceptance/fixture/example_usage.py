"""Runnable usage example and verification for event_summary.normalize_events."""

import copy
import json
from event_summary import normalize_events


def main():
    events = [
        {"pane_id": "%1", "kind": "output", "timestamp": 10.0, "text": "build "},
        {"pane_id": "%2", "kind": "output", "timestamp": 25.0, "text": "worker-start\n"},
        {"pane_id": "%1", "kind": "output", "timestamp": 8.5, "text": "done\n"},
        {"pane_id": "%2", "kind": "output", "timestamp": 20.0, "text": "worker-step\n"},
        {"pane_id": "%1", "kind": "exit", "timestamp": 9.0, "status": 0},
    ]
    original_snapshot = copy.deepcopy(events)

    result = normalize_events(events)

    expected = {
        "%1": {
            "output": "build done\n",
            "exit_status": 0,
            "last_timestamp": 10.0,
        },
        "%2": {
            "output": "worker-start\nworker-step\n",
            "exit_status": None,
            "last_timestamp": 25.0,
        },
    }

    assert result == expected, f"Mismatch:\nactual={result}\nexpected={expected}"
    assert events == original_snapshot, "Input events list was mutated"

    try:
        normalize_events([{"pane_id": "bad", "kind": "output", "timestamp": 1, "text": "x"}])
    except ValueError as exc:
        error_message = str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid pane_id")

    print("NORMALIZED_RESULT=" + json.dumps(result, sort_keys=True))
    print("INVALID_PANE_ERROR=" + error_message)
    print("VERIFICATION=PASS")


if __name__ == "__main__":
    main()
