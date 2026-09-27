"""OAuth credential loading and refresh."""

import http.client
import json
import os
import platform
import threading
import time
import urllib.parse

from .credentials import atomic_save_credentials, load_credentials


class TokenManager:
    def __init__(self, credentials_path, device_id_path, auth_endpoint,
                 client_id, device_info, logger, refresh_threshold=lambda: 300):
        self._credentials_path = credentials_path
        self._device_id_path = device_id_path
        self._auth_endpoint = auth_endpoint
        self._client_id = client_id
        self._logger = logger
        self._refresh_threshold = refresh_threshold
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._refreshing = False
        self._refresh_cond = threading.Condition(self._refresh_lock)
        self._data = {}
        self._mtime = None
        self._device_id = self._load_device_id()
        self._device_info = {
            **device_info,
            "os_version": device_info.get("os_version")
            or platform.mac_ver()[0] or "15.0",
            "device_id": self._device_id,
        }
        self._load()

    def _load_device_id(self):
        try:
            with open(self._device_id_path, encoding="utf-8") as source:
                return source.read().strip()
        except OSError:
            return "unknown"

    def _load(self):
        previous = self._data
        self._data, self._mtime = load_credentials(
            self._credentials_path, self._data, self._mtime, self._logger
        )
        if self._data is not previous:
            remaining = max(0, self._data.get("expires_at", 0) - time.time())
            self._logger.info("Token loaded, remaining %.0fs", remaining)

    def _maybe_reload(self):
        try:
            modified = os.path.getmtime(self._credentials_path)
        except OSError:
            return
        if self._mtime is None or modified != self._mtime:
            self._logger.info("Credentials file changed on disk, reloading")
            self._load()

    def _save(self):
        try:
            self._mtime = atomic_save_credentials(
                self._credentials_path, self._data
            )
        except OSError as error:
            self._logger.error("Save token failed: %s", error)

    def get_token(self):
        with self._lock:
            self._maybe_reload()
            return self._data.get("access_token", "")

    def get_headers(self):
        info = self._device_info
        return {
            "X-Msh-Platform": info["platform"],
            "X-Msh-Version": info["version"],
            "X-Msh-Device-Name": info["device_name"],
            "X-Msh-Device-Model": info["device_model"],
            "X-Msh-Os-Version": info["os_version"],
            "X-Msh-Device-Id": info["device_id"],
        }

    def should_refresh(self):
        with self._lock:
            self._maybe_reload()
            if not self._client_id or not self._data.get("refresh_token"):
                return False
            remaining = self._data.get("expires_at", 0) - time.time()
            return remaining < self._refresh_threshold()

    def status(self):
        with self._lock:
            expires_at = self._data.get("expires_at", 0)
            return {
                "token_expires_at": expires_at,
                "token_remaining": max(0, expires_at - time.time()),
            }

    def refresh(self):
        with self._refresh_lock:
            if self._refreshing:
                self._logger.info("Waiting for refresh...")
                self._refresh_cond.wait_for(
                    lambda: not self._refreshing, timeout=30
                )
                with self._lock:
                    return self._data.get("expires_at", 0) > time.time() + 10
            self._refreshing = True
        try:
            return self._perform_refresh()
        except Exception as error:
            self._logger.error("Token refresh failed: %s", error)
            return False
        finally:
            with self._refresh_lock:
                self._refreshing = False
                self._refresh_cond.notify_all()

    def _perform_refresh(self):
        with self._lock:
            self._maybe_reload()
            refresh_token = self._data.get("refresh_token", "")
            observed_mtime = self._mtime
            if not refresh_token:
                self._logger.warning("No refresh_token available")
                return False
        parsed = urllib.parse.urlparse(self._auth_endpoint)
        connection = http.client.HTTPSConnection(parsed.netloc, timeout=30)
        try:
            body = urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "refresh_token": refresh_token,
            })
            connection.request("POST", parsed.path, body=body, headers={
                "Content-Type": "application/x-www-form-urlencoded"
            })
            response = connection.getresponse()
            response_body = response.read()
        finally:
            connection.close()
        if response.status != 200:
            summary = response_body.decode("utf-8", errors="replace")[:200]
            self._logger.error("Token refresh HTTP %s: %s", response.status, summary)
            return False
        tokens = json.loads(response_body)
        with self._lock:
            self._maybe_reload()
            if (
                self._mtime != observed_mtime
                and self._data.get("expires_at", 0) > time.time() + 30
            ):
                self._logger.info("Credentials refreshed by another process")
                return True
            self._data.update({
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "token_type": tokens.get("token_type", "Bearer"),
                "expires_in": tokens.get("expires_in", 900),
                "expires_at": time.time() + tokens.get("expires_in", 900),
            })
            self._save()
        self._logger.info("Token refresh OK")
        return True
