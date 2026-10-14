"""Reusable Codex App Server child process."""

import os
import queue
import signal
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


def _descendant_pids(root_pid):
    """Snapshot direct and indirect children before their parent exits."""
    try:
        lines = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid="], text=True, timeout=3,
        ).splitlines()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    children = {}
    for line in lines:
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, parent = map(int, fields)
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
    descendants = []
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            descendants.append(child)
            pending.append(child)
    return descendants[::-1]


def _signal_pids(pids, signum):
    for pid in pids:
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


class AppServerProcess:
    """One reusable process; the owning pool serializes its callers."""

    def __init__(self):
        self.proc = None
        self.output = queue.Queue()
        self.completed_turns = 0
        self._process_group = None

    def start(self):
        if self.alive:
            return
        self.proc = subprocess.Popen(
            [codex(), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            env=_child_environment(),
            start_new_session=os.name == "posix",
        )
        self._process_group = self.proc.pid if os.name == "posix" else None
        self.completed_turns = 0
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
        proc = self.proc
        if proc is None:
            return
        group = self._process_group
        descendants = _descendant_pids(proc.pid) if os.name == "posix" else []
        self.proc = None
        self._process_group = None
        try:
            _signal_pids(descendants, signal.SIGTERM)
            if group is not None:
                try:
                    os.killpg(group, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        finally:
            # MCP children can start their own process groups, such as node_repl.
            _signal_pids(descendants, signal.SIGKILL)
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
