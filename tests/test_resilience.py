"""Resilience, cache-safety, and connection-reuse contracts."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("CAG_CLIENT_ID", "test-client")
os.environ.setdefault("CAG_CREDENTIALS_PATH", "/tmp/cag-resilience-missing.json")
os.environ.setdefault("CAG_DEVICE_ID_PATH", "/tmp/cag-resilience-device")
os.environ.setdefault("CAG_LOG_DIR", "/tmp/cag-resilience-logs")

from gateway.application.admission import AdmissionController
from gateway.cache.response import ResponseCache
from gateway.metrics import Metrics
from gateway.provider.codex import CodexProvider
from gateway.provider.credentials import (
    atomic_save_credentials, load_credentials,
)
from gateway.provider.upstream import UpstreamClient
from gateway.runtime import RuntimeState


class AdmissionTests(unittest.TestCase):
    def test_lease_tracks_and_releases_active_request(self):
        runtime = RuntimeState()
        admission = AdmissionController(runtime, 1)
        lease, _ = admission.acquire(0.1)
        self.assertIsNotNone(lease)
        with lease:
            self.assertEqual(runtime.request_count(), 1)
            blocked, _ = admission.acquire(0.01)
            self.assertIsNone(blocked)
        self.assertEqual(runtime.request_count(), 0)
        next_lease, _ = admission.acquire(0.1)
        self.assertIsNotNone(next_lease)
        with next_lease:
            pass

    def test_close_rejects_new_requests(self):
        admission = AdmissionController(RuntimeState(), 1)
        admission.close()
        lease, _ = admission.acquire(0.1)
        self.assertIsNone(lease)


class CacheSafetyTests(unittest.TestCase):
    def setUp(self):
        self.cache = ResponseCache(ttl=60, max_entries=10)
        self.base = {
            "model": "k3",
            "temperature": 0,
            "messages": [{"role": "user", "content": "hello"}],
        }

    def test_nondeterministic_request_is_not_cached(self):
        body = {**self.base, "temperature": 1}
        self.cache.put("/v1/chat/completions", body, b"x", [], 200)
        self.assertIsNone(
            self.cache.get_equivalent("/v1/chat/completions", body)
        )

    def test_all_request_fields_participate_in_key(self):
        self.cache.put(
            "/v1/chat/completions", self.base, b"x",
            [("Content-Type", "application/json")], 200,
        )
        changed = {**self.base, "stop": ["END"]}
        self.assertIsNone(
            self.cache.get_equivalent("/v1/chat/completions", changed)
        )


class CredentialSafetyTests(unittest.TestCase):
    def test_atomic_save_and_invalid_reload_preserve_last_valid_copy(self):
        logger = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "credentials.json")
            data = {"access_token": "safe", "expires_at": 123}
            mtime = atomic_save_credentials(path, data)
            loaded, loaded_mtime = load_credentials(
                path, {}, None, logger
            )
            self.assertEqual(loaded, data)
            self.assertEqual(loaded_mtime, mtime)
            Path(path).write_text("{broken", encoding="utf-8")
            preserved, preserved_mtime = load_credentials(
                path, loaded, loaded_mtime, logger
            )
            self.assertIs(preserved, loaded)
            self.assertEqual(preserved_mtime, loaded_mtime)


class ModelDiscoveryTests(unittest.TestCase):
    def test_stale_model_discovery_does_not_block_request(self):
        started, release = threading.Event(), threading.Event()

        def discover():
            started.set()
            release.wait(1)
            return ["gpt-test"]

        provider = CodexProvider(Mock())
        with patch("gateway.provider.codex.discover_models", discover):
            before = time.monotonic()
            initial = provider.model_records()
            elapsed = time.monotonic() - before
            self.assertLess(elapsed, 0.1)
            self.assertTrue(started.wait(0.5))
            self.assertFalse(any(x["id"] == "codex/gpt-test" for x in initial))
            release.set()
            for _ in range(20):
                records = provider.model_records()
                if any(x["id"] == "codex/gpt-test" for x in records):
                    break
                time.sleep(0.01)
            self.assertTrue(any(x["id"] == "codex/gpt-test" for x in records))
            self.assertFalse(any(x["id"] == "codex-spark" for x in records))

    def test_aliases_are_limited_to_discovered_models(self):
        provider = CodexProvider(Mock())
        provider._models = ("gpt-5.6-luna",)
        provider._models_at = time.time()
        names = {item["id"] for item in provider.model_records()}
        self.assertIn("codex-luna", names)
        self.assertNotIn("codex-spark", names)


class FakeResponse:
    status = 200
    will_close = False

    def getheader(self, _name):
        return None

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.requests = 0
        self.closed = False

    def request(self, *_args, **_kwargs):
        self.requests += 1

    def getresponse(self):
        return FakeResponse()

    def close(self):
        self.closed = True


class ConnectionPoolTests(unittest.TestCase):
    def test_connection_is_reused_after_response_release(self):
        connection = FakeConnection()
        client = UpstreamClient(
            timeout=lambda: 1, max_retries=lambda: 0,
            backoff_base=lambda: 0, logger=Mock(), metrics=Metrics(),
        )
        with patch.object(
            client._connections, "_create", return_value=connection
        ) as create:
            first, error = client.request("GET", "https://example.com/a", b"", {})
            self.assertIsNone(error)
            client.release(first)
            second, error = client.request("GET", "https://example.com/b", b"", {})
            self.assertIsNone(error)
            client.release(second)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(connection.requests, 2)
        client.close()


if __name__ == "__main__":
    unittest.main()
