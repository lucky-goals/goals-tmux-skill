#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage: bootstrap.sh [--check|--apply|--help]

Check that Python 3.12+ and tmux 3.6a+ are available. --check is read-only.
--apply is a backward-compatible check-only alias; it installs nothing.
Install required versions with your platform package manager, then rerun --check.
Set GOALS_TMUX_PYTHON to choose the Python interpreter (an executable path may
contain spaces).
EOF
}

if [ "$#" -gt 1 ]; then
  usage >&2
  exit 2
fi

mode=--check
if [ "$#" -eq 1 ]; then
  mode=$1
fi

case "$mode" in
  --check) ;;
  --apply)
    echo 'NOTICE: --apply is a backward-compatible check-only alias; it installs NOTHING.'
    echo 'Install Python 3.12+ and tmux 3.6a+ with your platform package manager, then rerun --check.'
    ;;
  --help|-h)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

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
      echo 'Install Python 3.12+ or set GOALS_TMUX_PYTHON to its executable path.' >&2
      exit 1
    fi
    ;;
esac

if python_result=$("$python_bin" -c '
import sys

version = ".".join(str(part) for part in sys.version_info[:3])
if sys.version_info < (3, 12):
    print("PYTHON_OLD:" + version)
    raise SystemExit(1)
print("PYTHON_OK:" + version)
' 2>&1); then
  case "$python_result" in
    PYTHON_OK:*) ;;
    *)
      echo "Could not validate Python interpreter '$python_bin': $python_result" >&2
      exit 1
      ;;
  esac
else
  python_status=$?
  case "$python_result" in
    PYTHON_OLD:*)
      python_found=${python_result#PYTHON_OLD:}
      echo "Python 3.12+ is required; '$python_bin' reports Python $python_found." >&2
      echo 'Install Python 3.12+ or set GOALS_TMUX_PYTHON to a newer interpreter.' >&2
      ;;
    *)
      echo "Could not run Python interpreter '$python_bin' (exit $python_status): $python_result" >&2
      echo 'Check the selected executable or set GOALS_TMUX_PYTHON to a working Python 3.12+ interpreter.' >&2
      ;;
  esac
  exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo 'tmux was not found on PATH; tmux 3.6a or later is required.' >&2
  echo 'Install or upgrade tmux with your platform package manager, then rerun --check.' >&2
  exit 1
fi

if tmux_version=$(tmux -V 2>&1); then
  :
else
  tmux_status=$?
  echo "Could not run 'tmux -V' (exit $tmux_status): $tmux_version" >&2
  echo 'Check the tmux executable on PATH and install tmux 3.6a or later.' >&2
  exit 1
fi

if tmux_result=$("$python_bin" -c '
import re
import sys

raw = sys.argv[1]
match = re.fullmatch(r"tmux ([0-9]+)\.([0-9]+)([a-z]?)", raw.strip())
if match is None:
    print("TMUX_UNKNOWN:" + raw)
    raise SystemExit(2)

major, minor = (int(match.group(1)), int(match.group(2)))
letter = match.group(3)
version_key = (major, minor, 1 if letter else 0, ord(letter) if letter else 0)
minimum_key = (3, 6, 1, ord("a"))
if version_key < minimum_key:
    print("TMUX_OLD:" + raw)
    raise SystemExit(1)
print("TMUX_OK")
' "$tmux_version" 2>&1); then
  if [ "$tmux_result" != TMUX_OK ]; then
    echo "Could not validate tmux version output '$tmux_version': $tmux_result" >&2
    exit 1
  fi
else
  tmux_status=$?
  case "$tmux_result" in
    TMUX_OLD:*)
      echo "tmux 3.6a or later is required; found '${tmux_result#TMUX_OLD:}'." >&2
      echo 'Upgrade tmux with your platform package manager, then rerun --check.' >&2
      ;;
    TMUX_UNKNOWN:*)
      echo "Could not parse 'tmux -V' output '${tmux_result#TMUX_UNKNOWN:}'; expected 'tmux MAJOR.MINOR' with an optional lowercase letter suffix." >&2
      echo 'Install a supported tmux version (3.6a or later) with your platform package manager.' >&2
      ;;
    *)
      echo "Could not validate tmux version (exit $tmux_status): $tmux_result" >&2
      exit 1
      ;;
  esac
  exit 1
fi

echo "Runtime ready: Python 3.12+ and $tmux_version available; Python standard library only."
