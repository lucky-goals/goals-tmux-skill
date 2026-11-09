#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage: setup-workflow.sh [--scan] [--json] [--project-dir PATH] [--probe-timeout SECONDS] [--help]

Interactively generate a proposed workflow YAML file. --scan and --json only
inspect supported CLI installations/model catalogs; they never enter prompts or
write configuration. Python 3.12+ is required. tmux is not required.

Set GOALS_TMUX_PYTHON to choose the Python interpreter (paths may contain spaces).
EOF
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
runtime_dir="$script_dir/../runtime"
python_bin=${GOALS_TMUX_PYTHON:-python3}

case "$python_bin" in
  */*)
    if [ ! -x "$python_bin" ]; then
      echo "Python interpreter '$python_bin' is missing or not executable." >&2
      echo 'Install Python 3.12+ or set GOALS_TMUX_PYTHON to its executable path.' >&2
      exit 1
    fi
    ;;
  *)
    if ! command -v "$python_bin" >/dev/null 2>&1; then
      echo "Python interpreter '$python_bin' was not found on PATH." >&2
      echo 'Install Python 3.12+ or set GOALS_TMUX_PYTHON to a newer interpreter.' >&2
      exit 1
    fi
    ;;
esac

if python_version=$("$python_bin" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>&1); then
  :
else
  status=$?
  echo "Could not run Python interpreter '$python_bin' (exit $status): $python_version" >&2
  exit 1
fi
if ! "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
  echo "Python 3.12+ is required; '$python_bin' reports Python $python_version." >&2
  echo 'Install Python 3.12+ or set GOALS_TMUX_PYTHON to a newer interpreter.' >&2
  exit 1
fi

if [ -n "${PYTHONPATH:-}" ]; then
  PYTHONPATH="$runtime_dir:$PYTHONPATH"
else
  PYTHONPATH=$runtime_dir
fi
export PYTHONPATH

exec "$python_bin" -m goals_tmux.setup_wizard "$@"
