#!/bin/bash
# Quick start script for Code Agent Gateway

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if present (safely handles values with spaces)
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Force direct connection — never depend on Clash or any system proxy
unset HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy

# Reduce memory fragmentation under high-load / large-request scenarios
export MALLOC_ARENA_MAX=2

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
for candidate in /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -z "$PYTHON_BIN" ] && [ -x "$candidate" ]; then PYTHON_BIN="$candidate"; fi
done
if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: python3 not found in PATH or standard locations." >&2
    exit 1
fi
echo "Starting Code Agent Gateway with $PYTHON_BIN..."
exec "$PYTHON_BIN" gateway_server.py
