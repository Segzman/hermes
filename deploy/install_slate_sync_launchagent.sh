#!/bin/zsh
set -euo pipefail

PLIST_SRC="/Users/sekun/hermes/deploy/com.hermes.slate-sync.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.hermes.slate-sync.plist"

mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"

launchctl bootout "gui/$(id -u)" "$PLIST_DST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/com.hermes.slate-sync"
launchctl kickstart -k "gui/$(id -u)/com.hermes.slate-sync"

echo "Installed launch agent: $PLIST_DST"
echo "Logs:"
echo "  $HOME/Library/Logs/hermes-slate-sync.log"
echo "  $HOME/Library/Logs/hermes-slate-sync.err.log"
