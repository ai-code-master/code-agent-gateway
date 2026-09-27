"""Best-effort discovery of models exposed by the local Codex app server."""
import json
import queue
import subprocess
import threading
import time

try:
    from .paths import codex
except ImportError:  # direct script compatibility
    from paths import codex


def _reader(stream, output):
    for line in stream:
        output.put(line)
    output.put(None)


def discover(timeout=8):
    """Return model ids, or an empty list when this Codex version lacks discovery."""
    proc = subprocess.Popen([codex(), 'app-server', '--listen', 'stdio://'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    output = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, output), daemon=True).start()

    def send(payload):
        proc.stdin.write(json.dumps(payload) + '\n')
        proc.stdin.flush()

    try:
        send({'method': 'initialize', 'id': 1, 'params': {
            'clientInfo': {'name': 'code-agent-gateway', 'version': '0.3'},
            'capabilities': {},
        }})
        send({'method': 'initialized', 'params': {}})
        request_id = 2
        cursor = None
        send({'method': 'model/list', 'id': request_id,
              'params': {'includeHidden': False}})
        deadline = time.monotonic() + timeout
        models = []
        while time.monotonic() < deadline:
            try:
                line = output.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get('id') == request_id:
                data = message.get('result', {}).get('data', [])
                models.extend(item['id'] for item in data
                              if isinstance(item, dict) and item.get('id'))
                cursor = message.get('result', {}).get('nextCursor')
                if not cursor:
                    return list(dict.fromkeys(models))
                request_id += 1
                send({'method': 'model/list', 'id': request_id,
                      'params': {'includeHidden': False, 'cursor': cursor}})
        return list(dict.fromkeys(models))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
