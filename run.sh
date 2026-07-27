#!/usr/bin/env bash
# Local runner for the arxiv+DBLP tracker.
# Creates a venv once, installs deps, then runs tracker.py with current env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -d .venv ]]; then
  echo "[setup] creating venv (python 3.11)..."
  uv venv --python 3.11 .venv
  source .venv/bin/activate
  uv pip install -r requirements.txt
else
  source .venv/bin/activate
fi

exec python src/tracker.py "$@"
