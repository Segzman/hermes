#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${2:-$ROOT_DIR/.env}"
SSM_PREFIX="${1:-/hermes/prod}"

python3 - <<'PY' "$ENV_FILE" "$SSM_PREFIX"
import subprocess
import sys
from pathlib import Path

env_file = Path(sys.argv[1]).expanduser()
prefix = sys.argv[2].rstrip("/")

SYNC_KEYS = [
    "LLM_PROVIDER",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "BEDROCK_API_KEY",
    "BEDROCK_BASE_URL",
    "BEDROCK_MODEL",
    "BEDROCK_VISION_MODEL",
    "VOICE_TRANSCRIBE_MODEL",
    "SERPER_API_KEY",
    "SLATE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "APPLE_ID",
    "APPLE_APP_PASSWORD",
    "APPLE_REMINDERS_NAME",
    "APPLE_CALENDAR_NAME",
    "BROWSER_BACKEND",
    "BROWSERBASE_API_KEY",
    "BROWSERBASE_PROJECT_ID",
    "BROWSERBASE_KEEP_ALIVE",
    "BROWSERBASE_CONTEXT_ID",
    "CHECK_INTERVAL_MINUTES",
    "REMINDER_DAYS_AHEAD",
]


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        raise SystemExit(f"Missing env file: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
            value = value[1:-1]
        values[key] = value
    return values


env_values = load_env(env_file)

for key in SYNC_KEYS:
    value = env_values.get(key, "").strip()
    if not value:
        continue
    name = f"{prefix}/{key}"
    print(f"Uploading {name}")
    subprocess.run(
        [
            "aws",
            "ssm",
            "put-parameter",
            "--name",
            name,
            "--type",
            "SecureString",
            "--overwrite",
            "--value",
            value,
        ],
        check=True,
    )
PY
