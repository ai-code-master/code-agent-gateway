#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PLIST_SRC="$SCRIPT_DIR/codex-workbuddy-bridge.plist"
PLIST_DST="$HOME/Library/LaunchAgents/io.github.codex-workbuddy-bridge.plist"
PYTHON3_PATH="$(which python3)"

mkdir -p "$HOME/.workbuddy/logs"
sed -e "s|/ABSOLUTE/PATH/TO/kimi-code-proxy|$PROJECT_DIR|g" \
    -e "s|/PATH/TO/python3|$PYTHON3_PATH|g" \
    -e "s|/Users/YOUR_USERNAME|$HOME|g" \
    "$PLIST_SRC" > "$PLIST_DST"

launchctl bootout "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
echo "Codex bridge installed at http://127.0.0.1:8766"
