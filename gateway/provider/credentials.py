"""Safe JSON credential persistence shared with local coding agents."""

import json
import os
import tempfile


def load_credentials(path, previous, previous_mtime, logger):
    try:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        if not isinstance(data, dict):
            raise ValueError("credential payload must be an object")
        return data, os.path.getmtime(path)
    except (OSError, ValueError) as error:
        log = logger.warning if previous else logger.error
        log("Load credentials failed; keeping last valid copy: %s", error)
        return previous, previous_mtime


def atomic_save_credentials(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    mode = 0o600
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        pass
    descriptor, temporary = tempfile.mkstemp(
        prefix=".code-agent-gateway-", suffix=".tmp", dir=directory
    )
    try:
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(data, target, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        return os.path.getmtime(path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
