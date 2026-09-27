"""Bounded file logging for the local gateway."""

import logging
import os
import sys
import threading


class BoundedRotatingHandler(logging.Handler):
    def __init__(self, filename, max_bytes, backup_count, directory_limit):
        super().__init__()
        self.filename = filename
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.directory_limit = directory_limit
        self._lock = threading.Lock()
        self.stream = None
        self._open()

    def _open(self):
        if self.stream:
            self.stream.close()
        self.stream = open(self.filename, "a", encoding="utf-8")

    def _rotate(self):
        if not os.path.exists(self.filename):
            return
        if os.path.getsize(self.filename) < self.max_bytes:
            return
        self.stream.close()
        for index in range(self.backup_count - 1, 0, -1):
            source = f"{self.filename}.{index}"
            target = f"{self.filename}.{index + 1}"
            if os.path.exists(source):
                os.replace(source, target)
        os.replace(self.filename, f"{self.filename}.1")
        self._open()

    def _trim_directory(self):
        try:
            entries = []
            total = 0
            prefix = os.path.basename(self.filename)
            directory = os.path.dirname(self.filename)
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if os.path.isfile(path) and name.startswith(prefix):
                    size = os.path.getsize(path)
                    total += size
                    entries.append((path, os.path.getmtime(path), size))
            for path, _, size in sorted(entries, key=lambda item: item[1]):
                if total <= self.directory_limit * 0.8:
                    break
                if path != self.filename:
                    os.remove(path)
                    total -= size
        except OSError:
            return

    def emit(self, record):
        try:
            with self._lock:
                self._trim_directory()
                self._rotate()
                self.stream.write(self.format(record) + "\n")
                self.stream.flush()
        except Exception:
            self.handleError(record)


def build_logger(log_dir, log_file, max_bytes, backup_count, directory_limit):
    os.makedirs(log_dir, exist_ok=True)
    handler = BoundedRotatingHandler(
        log_file, max_bytes, backup_count, directory_limit
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger = logging.getLogger("code-agent-gateway")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(handler)
    return logger


def install_exception_hook(logger):
    def log_uncaught(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.error(
            "Uncaught exception: %s", exc_value,
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    sys.excepthook = log_uncaught
