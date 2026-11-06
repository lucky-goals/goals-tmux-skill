# `event_summary` Usage Guide

`normalize_events(events)` aggregates an iterable of terminal pane event dictionaries into a per-pane summary dictionary without mutating the input sequence.

## Function Signature & Return Structure

```python
from event_summary import normalize_events

summary = normalize_events(events)
```

Returns a `dict` keyed by `pane_id`:

```python
{
    "%1": {
        "output": "joined output text in event arrival order",
        "exit_status": 0,          # int from the latest "exit" event, or None
        "last_timestamp": 12.5,    # maximum timestamp seen for this pane
    }
}
```

- **`output`**: Concatenation of all `"output"` event `text` values for that pane in stream order.
- **`exit_status`**: `None` initially; updated to `record["status"]` whenever an `"exit"` event is processed for that pane.
- **`last_timestamp`**: The maximum `timestamp` observed across all events for that pane (even if timestamps arrive out of order).

## Event Schema

Every item in `events` must be a `dict` with the following fields:

| Field | Type / Constraint | Required |
| :--- | :--- | :--- |
| `pane_id` | `str` matching `^%[0-9]+$` (e.g., `"%1"`, `"%2"`) | Always |
| `kind` | `"output"` or `"exit"` | Always |
| `timestamp` | Finite `int` or `float` (`>= 0`, `bool` rejected) | Always |
| `text` | `str` | When `kind == "output"` |
| `status` | `int` (`bool` rejected) | When `kind == "exit"` |

## `ValueError` Behavior

`normalize_events` validates each record before updating state and raises `ValueError` (identifying the 0-based event index) when:

- A record is not a `dict`.
- `pane_id` is missing, not a `str`, or does not full-match `%[0-9]+`.
- `kind` is not `"output"` or `"exit"`.
- `timestamp` is missing, a `bool`, non-numeric, negative, `NaN`, or infinite.
- `kind == "output"` and `text` is missing or not a `str`.
- `kind == "exit"` and `status` is missing, not an `int`, or is a `bool`.

## Runnable Example

Run `python3 example_usage.py` to execute an example with two interleaved panes (`%1` and `%2`), out-of-order timestamps, an exit event, and independent verification checks.
