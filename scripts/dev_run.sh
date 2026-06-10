#!/usr/bin/env bash
# Dev runner: mimics systemd Restart=always so the dashboard Restart button
# works locally — the process exits, we relaunch it.
set -u
cd "$(dirname "$0")/.."

# Use the project venv (create + install it on first run)
if [ ! -x .venv/bin/python ]; then
  echo "No .venv found — creating it and installing dependencies..."
  python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]' || {
    echo "venv setup failed" >&2
    exit 1
  }
fi
PYTHON=.venv/bin/python

while true; do
  "$PYTHON" -m saintantoine --mock "$@"
  code=$?
  echo "saintantoine exited with code $code — restarting in 2s (Ctrl-C to stop)"
  sleep 2
done
