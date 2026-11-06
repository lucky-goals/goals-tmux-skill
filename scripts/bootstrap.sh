#!/bin/sh
set -eu
case "${1:---check}" in
  --check|--apply) ;;
  *) echo 'usage: bootstrap.sh [--check|--apply]' >&2; exit 2 ;;
esac
"${GOALS_TMUX_PYTHON:-python3}" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ is required"'
if ! command -v tmux >/dev/null 2>&1; then
  echo 'tmux is missing; install tmux 3.6a or later using your platform package manager.' >&2
  exit 1
fi
tmux -V
echo 'Runtime ready: Python standard library only; no environment installation is needed.'
