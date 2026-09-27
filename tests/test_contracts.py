"""Public protocol and provider lifecycle contracts."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("CAG_CLIENT_ID", "test-client")
os.environ.setdefault("CAG_CREDENTIALS_PATH", "/tmp/cag-contract-missing.json")
os.environ.setdefault("CAG_DEVICE_ID_PATH", "/tmp/cag-contract-device")
os.environ.setdefault("CAG_LOG_DIR", "/tmp/cag-contract-logs")

from codex_bridge.appserver import AppServerPool
from codex_bridge.process import _child_environment
from gateway.compat import chat_to_response, responses_to_chat
from gateway.config import GatewayConfig
from gateway.provider.health import UpstreamHealth


class ResponsesContractTests(unittest.TestCase):
    def test_request_maps_instructions_reasoning_and_tools(self):
        body = {
            "model": "codex-sol",
            "instructions": "Be concise",
            "input": "hello",
            "max_output_tokens": 64,
            "reasoning": {"effort": "high"},
            "tools": [{
                "type": "function",
                "name": "lookup",
                "description": "Lookup a value",
                "parameters": {"type": "object"},
            }],
        }
        chat = responses_to_chat(body)
        self.assertEqual(chat["messages"][0]["role"], "system")
        self.assertEqual(chat["messages"][1]["content"], "hello")
        self.assertEqual(chat["max_tokens"], 64)
        self.assertEqual(chat["reasoning_effort"], "high")
        self.assertEqual(chat["tools"][0]["function"]["name"], "lookup")

    def test_chat_completion_maps_to_response(self):
        chat = {
            "choices": [{"message": {
                "role": "assistant", "content": "OK",
            }}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        }
        response = chat_to_response(chat, {"model": "codex-sol"})
        self.assertEqual(response["object"], "response")
        self.assertEqual(response["output"][0]["content"][0]["text"], "OK")
        self.assertEqual(response["usage"]["total_tokens"], 3)


class ConfigurationContractTests(unittest.TestCase):
    def test_legacy_prefix_is_not_read(self):
        with patch.dict(os.environ, {
            "CAG_CLIENT_ID": "",
            "KCP_CLIENT_ID": "legacy-value",
        }):
            config = GatewayConfig.from_env()
        self.assertEqual(config.client_id, "")
        config.validate()


class FakeProcess:
    starts = 0

    def __init__(self):
        self.proc = None
        self._alive = False

    @property
    def alive(self):
        return self._alive

    def start(self):
        if not self._alive:
            self._alive = True
            FakeProcess.starts += 1

    def run(self, prompt, cwd, **_kwargs):
        self.start()
        return prompt, cwd

    def close(self):
        self._alive = False


class ProcessPoolTests(unittest.TestCase):
    def test_codex_proxy_is_scoped_to_child_environment(self):
        proxy = "http://127.0.0.1:7897"
        with patch.dict(os.environ, {"CAG_CODEX_PROXY": proxy}):
            child_env = _child_environment()
        self.assertEqual(child_env["HTTPS_PROXY"], proxy)
        self.assertEqual(child_env["https_proxy"], proxy)

    def test_process_is_reused_between_requests(self):
        FakeProcess.starts = 0
        with patch("codex_bridge.appserver.AppServerProcess", FakeProcess):
            pool = AppServerPool(1)
            self.assertEqual(pool.run("one", "/tmp"), ("one", "/tmp"))
            self.assertEqual(pool.run("two", "/tmp"), ("two", "/tmp"))
            self.assertEqual(FakeProcess.starts, 1)
            self.assertEqual(pool.stats()["processes"], 1)
            pool.close()


class ProviderHealthTests(unittest.TestCase):
    def test_codex_failure_degrades_combined_health(self):
        health = UpstreamHealth(codex_probe=lambda: False)
        health._check_interval = 1
        health._do_probe = lambda: "healthy"
        self.assertFalse(health.check())
        self.assertTrue(health.is_ready())
        self.assertEqual(
            health.provider_status(), {"kimi": True, "codex": False}
        )


if __name__ == "__main__":
    unittest.main()
