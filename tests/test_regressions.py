import os
import queue
import json
import sys
import threading
import time
import unittest


os.environ.setdefault("KCP_CLIENT_ID", "test-client")
os.environ.setdefault("KCP_CREDENTIALS_PATH", "/tmp/kcp-test-missing-credentials.json")
os.environ.setdefault("KCP_DEVICE_ID_PATH", "/tmp/kcp-test-missing-device")
os.environ.setdefault("KCP_LOG_DIR", "/tmp/kcp-test-logs")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "codex_bridge"))

import gateway_server as proxy
from appserver import _await_turn
from gateway.config import GatewayConfig
from gateway.provider.codex import CodexProvider
from gateway.runtime import RuntimeState


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.old_cache = proxy.ENABLE_CACHE
        self.old_semantic = proxy.ENABLE_SEMANTIC_CACHE
        proxy.ENABLE_CACHE = True
        proxy.ENABLE_SEMANTIC_CACHE = True
        self.cache = proxy.ResponseCache(ttl=60, max_entries=10)
        self.base = {
            "model": "k3",
            "messages": [
                {"role": "system", "content": "same context"},
                {"role": "user", "content": "hello world"},
            ],
        }
        self.cache.put(
            "/v1/chat/completions", self.base, b"answer",
            [("Content-Type", "application/json")], 200,
        )

    def tearDown(self):
        proxy.ENABLE_CACHE = self.old_cache
        proxy.ENABLE_SEMANTIC_CACHE = self.old_semantic

    def test_normalized_equivalence_hits(self):
        request = {
            "model": "k3",
            "messages": [
                {"role": "system", "content": "same context"},
                {"role": "user", "content": "  hello world\r\n"},
            ],
        }
        self.assertEqual(
            self.cache.get_semantic("/v1/chat/completions", request)[0],
            b"answer",
        )

    def test_changed_prompt_or_context_misses(self):
        changed = {
            "model": "k3",
            "messages": [
                {"role": "system", "content": "same context"},
                {"role": "user", "content": "hello worlds"},
            ],
        }
        changed_context = {
            "model": "k3",
            "messages": [
                {"role": "system", "content": "different context"},
                {"role": "user", "content": "hello world"},
            ],
        }
        self.assertIsNone(self.cache.get_semantic("/v1/chat/completions", changed))
        self.assertIsNone(
            self.cache.get_semantic("/v1/chat/completions", changed_context)
        )


class GatewayBoundaryTests(unittest.TestCase):
    def test_config_reads_legacy_environment_contract(self):
        config = GatewayConfig.from_env()
        self.assertEqual(config.client_id, "test-client")
        self.assertEqual(config.host, "127.0.0.1")

    def test_runtime_tracks_active_requests(self):
        state = RuntimeState()
        self.assertEqual(state.begin_request(), 1)
        self.assertEqual(state.end_request(), 0)


class SingleFlightTests(unittest.TestCase):
    def test_concurrent_calls_share_one_result(self):
        old = proxy.ENABLE_SINGLE_FLIGHT
        proxy.ENABLE_SINGLE_FLIGHT = True
        try:
            flight = proxy.SingleFlight()
            barrier = threading.Barrier(3)
            calls = []
            results = []

            def operation():
                calls.append(1)
                time.sleep(0.1)
                return "ok"

            def worker():
                barrier.wait()
                results.append(flight.do(
                    "POST", "/v1/chat/completions", b"{}", operation
                ))

            threads = [threading.Thread(target=worker) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(calls), 1)
            self.assertEqual(sum(coalesced for _, coalesced in results), 2)
        finally:
            proxy.ENABLE_SINGLE_FLIGHT = old


class HealthTests(unittest.TestCase):
    def test_transient_failure_is_debounced_and_401_is_not(self):
        health = proxy.UpstreamHealth()
        health._check_interval = 0
        results = iter(["failure", "failure", "healthy", "auth_failure"])
        health._do_probe = lambda: next(results)
        self.assertEqual(
            [health.check(), health.check(), health.check(), health.check()],
            [True, False, True, False],
        )


class CodexStreamingTests(unittest.TestCase):
    def test_codex_model_routing_is_explicit(self):
        self.assertTrue(CodexProvider.handles("codex-sol"))
        self.assertTrue(CodexProvider.handles("codex/gpt-5.6-sol"))
        self.assertFalse(CodexProvider.handles("kimi-for-coding"))

    def test_codex_alias_resolves_model_and_effort(self):
        self.assertEqual(
            CodexProvider._resolve("codex-terra", None),
            ("gpt-5.6-terra", "medium"),
        )

    def test_only_final_answer_deltas_are_streamed(self):
        events = queue.Queue()
        messages = [
            {"method": "item/started", "params": {"item": {
                "id": "comment", "type": "agentMessage", "phase": "commentary"}}},
            {"method": "item/agentMessage/delta", "params": {
                "itemId": "comment", "delta": "hidden"}},
            {"method": "item/started", "params": {"item": {
                "id": "final", "type": "agentMessage", "phase": "final_answer"}}},
            {"method": "item/agentMessage/delta", "params": {
                "itemId": "final", "delta": "A"}},
            {"method": "item/agentMessage/delta", "params": {
                "itemId": "final", "delta": "B"}},
            {"method": "item/completed", "params": {"item": {
                "id": "final", "type": "agentMessage",
                "phase": "final_answer", "text": "AB"}}},
            {"method": "turn/completed", "params": {"turn": {
                "status": "completed"}}},
        ]
        for message in messages:
            events.put(json.dumps(message))
        deltas = []
        result = _await_turn(events, 2, deltas.append)
        self.assertEqual(deltas, ["A", "B"])
        self.assertEqual(result.content, "AB")


if __name__ == "__main__":
    unittest.main()
