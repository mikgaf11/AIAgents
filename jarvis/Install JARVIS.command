#!/usr/bin/env bash
# Double-click this on macOS or Linux to set up JARVIS.
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "Python 3 is required. Install it from https://python.org and run this again."
  read -r -p "Press Enter to close…" _
  exit 1
fi
"$PY" install.py
echo
read -r -p "Press Enter to close…" _
