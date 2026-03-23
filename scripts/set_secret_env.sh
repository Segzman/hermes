#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${HERMES_ENV_FILE:-$ROOT_DIR/.env}"
VAR_NAME="${1:-}"

if [[ -z "$VAR_NAME" ]]; then
    echo "Usage: $0 VAR_NAME"
    exit 1
fi

read -r -s -p "Value for $VAR_NAME: " VAR_VALUE
echo

python3 - <<'PY' "$ENV_FILE" "$VAR_NAME" "$VAR_VALUE"
from pathlib import Path
import sys

env_file = Path(sys.argv[1]).expanduser()
name = sys.argv[2]
value = sys.argv[3]

if env_file.exists():
    lines = env_file.read_text(encoding="utf-8").splitlines()
else:
    lines = []

updated = False
new_lines = []
for line in lines:
    if line.startswith(f"{name}="):
        new_lines.append(f"{name}={value}")
        updated = True
    else:
        new_lines.append(line)

if not updated:
    if new_lines and new_lines[-1] != "":
        new_lines.append("")
    new_lines.append(f"{name}={value}")

env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
print(f"Saved {name} to {env_file}")
PY
