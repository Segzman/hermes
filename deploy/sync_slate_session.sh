#!/bin/zsh
set -euo pipefail

REPO_DIR="/Users/sekun/hermes"
LOG_DIR="$HOME/Library/Logs"
LOG_FILE="$LOG_DIR/hermes-slate-sync.log"
ERR_FILE="$LOG_DIR/hermes-slate-sync.err.log"

mkdir -p "$LOG_DIR"

cd "$REPO_DIR"
PYTHONPATH="$REPO_DIR" "$REPO_DIR/.venv-local/bin/python" -m slate.sync --skip-if-expired >>"$LOG_FILE" 2>>"$ERR_FILE"
