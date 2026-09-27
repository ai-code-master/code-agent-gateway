#!/usr/bin/env python3
"""Compatibility entrypoint for Code Agent Gateway."""

import sys
from pathlib import Path

from gateway.application import GatewayApplication
from gateway.cache import ResponseCache, SingleFlight
from gateway.provider import UpstreamHealth


try:
    application = GatewayApplication(Path(__file__).resolve().parent)
except ValueError as error:
    print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(1) from error

ProxyHandler = application.handler
ENABLE_CACHE = application.settings.current.enable_cache
ENABLE_EQUIVALENCE_CACHE = application.settings.current.enable_equivalence_cache
ENABLE_SINGLE_FLIGHT = application.settings.current.enable_single_flight


def main():
    application.run()


if __name__ == "__main__":
    main()
