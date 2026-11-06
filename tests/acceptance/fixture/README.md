# Four CLI acceptance snapshot

Recorded output from the 2026-11-06 live acceptance: Claude implemented and fixed the module, Codex wrote 19 tests, Cursor independently reviewed the final sources, and Antigravity wrote/ran the usage example. This is an acceptance fixture, not part of the shipped runtime.

To reproduce its focused checks, run from this directory:

```sh
python3 -m unittest test_event_summary -v
python3 example_usage.py
```

See `../../../docs/acceptance-2026-11-06.md` for runtime checks and resource settlement.
