#!/bin/bash
# Install launchd service for macOS

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PLIST_SRC="$SCRIPT_DIR/code-agent-gateway.plist"
PLIST_DST="$HOME/Library/LaunchAgents/io.github.code-agent-gateway.plist"

echo "Installing launchd service..."
echo "  Project dir: $PROJECT_DIR"

PYTHON3_PATH="$(which python3)"

# Substitute paths
sed -e "s|/ABSOLUTE/PATH/TO/code-agent-gateway|$PROJECT_DIR|g" \
    -e "s|/ABSOLUTE/PATH/TO/gateway_server.py|$PROJECT_DIR/gateway_server.py|g" \
    -e "s|/PATH/TO/python3|$PYTHON3_PATH|g" \
    -e "s|/Users/YOUR_USERNAME|$HOME|g" \
    -e "s|io.github.YOUR_USERNAME.code-agent-gateway|io.github.code-agent-gateway|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# Stop only the matching launchd service. Manual processes are left untouched.
launchctl unload "$PLIST_DST" 2>/dev/null || true
sleep 1

launchctl load "$PLIST_DST"
launchctl start io.github.code-agent-gateway

echo "Done! Check status with: launchctl list | grep code-agent-gateway"
