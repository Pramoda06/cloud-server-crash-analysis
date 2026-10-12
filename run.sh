#!/usr/bin/env bash
# Launcher for WSL / Linux / macOS: creates a virtual environment on first run,
# installs Flask + psutil, then starts the simulator.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  . .venv/bin/activate
  pip install -r requirements.txt
else
  . .venv/bin/activate
fi
python app.py "$@"
