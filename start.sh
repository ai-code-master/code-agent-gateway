#!/bin/bash
# Quick start script for Kimi Code Proxy

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if present (safely handles values with spaces)
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Validate required env
if [ -z "$KCP_CLIENT_ID" ]; then
    echo "ERROR: KCP_CLIENT_ID is not set. Please configure .env file."
    exit 1
fi

# Force direct connection — never depend on Clash or any system proxy
unset HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy

# Reduce memory fragmentation under high-load / large-request scenarios
export MALLOC_ARENA_MAX=2

echo "Starting Kimi Code Proxy..."
exec /opt/homebrew/bin/python3.11 kimi_code_proxy.py
