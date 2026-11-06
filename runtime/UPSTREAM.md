# Runtime provenance

The operation journal in `goals_tmux/operation_store.py` is adapted from
`goals-iterm-skill/runtime/goals_iterm/operation_store.py`, source commit
`918cd7b31481e2f9ea0a7e94f61e8f28b202206f` (MIT, copyright 2026 lucky-goals).
Its private file persistence, bounded locking, reservation and result semantics
are preserved. State paths refer to goals-tmux-skill instead of goals-iterm-skill.

The skill's lifecycle, file dispatch and audit protocols draw on the same
project. The tmux transport, endpoint/pane identity, shared-window topology,
configuration, viewer and Control Mode observer are separate implementations.

Original license notices are preserved in `LICENSE` and the repository license.
