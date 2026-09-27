"""Reusable Codex App Server child process."""

import os
import queue
import subprocess
import threading

from .paths import codex
from .protocol import await_turn, list_models, read_lines, send, start_thread


def _child_environment():
    """Build a Codex-only environment without proxying other providers."""
    env = os.environ.copy()
    proxy = os.environ.get("CAG_CODEX_PROXY", "").strip()
    if proxy:
        for name in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        ):
            env[name] = proxy
    return env


class AppServerProcess:
    """One reusable process; the owning pool serializes its callers."""

    def __init__(self):
        self.proc = None
        self.output = queue.Queue()

    def start(self):
        if self.alive:
            return
        self.proc = subprocess.Popen(
            [codex(), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            env=_child_environment(),
        )
        self.output = queue.Queue()
        threading.Thread(
            target=read_lines, args=(self.proc.stdout, self.output), daemon=True
        ).start()
        send(self.proc, {
            "method": "initialize", "id": 0,
            "params": {
                "clientInfo": {
                    "name": "code-agent-gateway",
                    "title": "Code Agent Gateway",
                    "version": "3.2.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        })
        send(self.proc, {"method": "initialized", "params": {}})

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def run(self, prompt, cwd, tools=None, effort=None, model=None,
            timeout=900, on_delta=None):
        self.start()
        thread_id = start_thread(
            self.proc, self.output, cwd, tools, model, timeout
        )
        params = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if effort in {"minimal", "low", "medium", "high", "xhigh", "max"}:
            params["effort"] = effort
        send(self.proc, {"method": "turn/start", "id": 2, "params": params})
        return await_turn(self.output, timeout, on_delta)

    def models(self, timeout=8):
        self.start()
        return list_models(self.proc, self.output, timeout)

    def close(self):
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
