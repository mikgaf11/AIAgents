#!/usr/bin/env bash
# Boot JARVIS: create a venv if needed, install deps, launch the core.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo "· creating virtual environment"
  "$PYTHON" -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
elif [ requirements.txt -nt .venv/pyvenv.cfg ]; then
  echo "· requirements changed, updating"
  ./.venv/bin/pip install --quiet -r requirements.txt
  touch .venv/pyvenv.cfg
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ ! -f .env ]; then
  echo "! no ANTHROPIC_API_KEY and no .env — starting in offline mode."
  echo "  cp .env.example .env and add your key to enable the reasoning core."
fi

exec ./.venv/bin/python -m server.main
