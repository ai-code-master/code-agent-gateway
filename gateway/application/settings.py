"""Environment-file loading and typed configuration snapshots."""

import os

from gateway.config import GatewayConfig


class SettingsStore:
    def __init__(self, base_dir):
        self._path = base_dir / ".env"
        self._dotenv_keys = set()
        self._load_dotenv(override=False)
        self.current = GatewayConfig.from_env()
        self.current.validate()

    def reload(self):
        self._load_dotenv(override=True)
        updated = GatewayConfig.from_env()
        updated.validate()
        self.current = updated
        return updated

    def _load_dotenv(self, override):
        try:
            values = {}
            with self._path.open(encoding="utf-8") as source:
                for raw_line in source:
                    line = raw_line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        values[key] = value.strip()
            if override:
                for key in self._dotenv_keys - values.keys():
                    os.environ.pop(key, None)
                    self._dotenv_keys.discard(key)
            for key, value in values.items():
                if key in self._dotenv_keys or key not in os.environ:
                    os.environ[key] = value
                    self._dotenv_keys.add(key)
        except OSError:
            return
