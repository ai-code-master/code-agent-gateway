"""In-process Codex provider backed by the local Codex App Server."""

import threading
import time

from codex_bridge.appserver import (
    AppServerError, pool_stats, probe_codex, run_codex,
)
from codex_bridge.catalog import discover
from codex_bridge.chat import build_prompt, detect_cwd, dynamic_tools
from codex_bridge.responses import completion


MODEL_MAP = {
    "codex-local": ("gpt-5.6-sol", "high"),
    "codex-spark": ("gpt-5.3-codex-spark", "high"),
    "codex-sol": ("gpt-5.6-sol", "high"),
    "codex-terra": ("gpt-5.6-terra", "medium"),
    "codex-luna": ("gpt-5.6-luna", "low"),
}
PUBLIC_MODELS = ("codex-spark", "codex-sol", "codex-terra", "codex-luna")


class CodexProvider:
    def __init__(self, logger):
        self._logger = logger
        self._models = ()
        self._models_at = 0.0
        self._model_lock = threading.Lock()

    @staticmethod
    def handles(model):
        return isinstance(model, str) and (
            model in MODEL_MAP or model.startswith("codex/")
            or model.startswith("custom-local:codex/")
        )

    def model_records(self):
        discovered = self._discover_models()
        names = tuple(dict.fromkeys(
            PUBLIC_MODELS + tuple(f"codex/{name}" for name in discovered)
        ))
        return [
            {"id": name, "object": "model", "owned_by": "openai"}
            for name in names
        ]

    def run(self, body, headers, on_delta=None):
        messages = body.get("messages") or []
        requested = body.get("model") or "codex-sol"
        model, effort = self._resolve(requested, body.get("reasoning_effort"))
        cwd = detect_cwd(messages, headers)
        tools = dynamic_tools(body.get("tools"))
        self._logger.info(
            "Codex request model=%s resolved=%s effort=%s cwd=%s",
            requested, model, effort, cwd,
        )
        result = run_codex(
            build_prompt(messages), cwd, tools=tools, effort=effort,
            model=model, on_delta=on_delta,
        )
        return result, requested

    def complete(self, body, headers):
        result, requested = self.run(body, headers)
        return completion(result, requested)

    @staticmethod
    def health():
        return probe_codex()

    @staticmethod
    def status():
        return pool_stats()

    @staticmethod
    def _resolve(requested, requested_effort):
        normalized = requested
        if normalized.startswith("custom-local:"):
            normalized = normalized[len("custom-local:"):]
        model, default_effort = MODEL_MAP.get(normalized, (None, "high"))
        if model is None:
            model = normalized.removeprefix("codex/") or "gpt-5.6-sol"
        return model, requested_effort or default_effort

    def _discover_models(self):
        with self._model_lock:
            if time.time() - self._models_at <= 60:
                return self._models
            try:
                self._models = tuple(discover())
            except Exception as error:
                self._logger.warning("Codex model discovery skipped: %s", error)
                self._models = ()
            self._models_at = time.time()
            return self._models


__all__ = ["AppServerError", "CodexProvider"]
