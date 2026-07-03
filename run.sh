#!/bin/bash
# Convenience launcher
set -e
cd "$(dirname "$0")"

# Resolve port (env var > default)
PORT="${LH_UI_PORT:-8082}"

# Free the port if something is already listening on it
if command -v fuser >/dev/null 2>&1; then
  if fuser -s "${PORT}/tcp" 2>/dev/null; then
    echo "Port ${PORT} is busy — killing previous process..."
    fuser -k "${PORT}/tcp" 2>/dev/null || true
    sleep 1
  fi
fi

if [ ! -d ".venv" ]; then
  echo "Creating venv..."
  python -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt
python -m src.main init
echo "Starting HunterBot Dashboard at http://localhost:${PORT}"
python -m ui.app
