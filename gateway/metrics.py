"""Thread-safe request and token metrics."""

from __future__ import annotations

import threading


class Metrics:
    def __init__(self, slow_threshold: float = 30.0):
        self._lock = threading.Lock()
        self.slow_threshold = slow_threshold
        self.request_count = self.error_count = self.timeout_count = 0
        self.retry_count = self.client_reset_count = 0
        self.slow_request_count = 0
        self.latency_sum = self.latency_count = 0.0
        self.status_counts = {}
        self.queue_wait_sum = self.queue_wait_count = 0.0
        self.model_counts = {}
        self.input_tokens_sum = self.output_tokens_sum = self.total_tokens_sum = 0
        self.token_record_count = 0

    def record_request(self, latency, status=None, queue_wait=0.0,
                       model="", tokens=None):
        with self._lock:
            self.request_count += 1
            self.latency_sum += latency
            self.latency_count += 1
            if queue_wait > 0:
                self.queue_wait_sum += queue_wait
                self.queue_wait_count += 1
            if status is not None:
                self.status_counts[status] = self.status_counts.get(status, 0) + 1
                if status >= 500 or status == 429:
                    self.error_count += 1
            if model:
                self.model_counts[model] = self.model_counts.get(model, 0) + 1
            if latency > self.slow_threshold:
                self.slow_request_count += 1
            if tokens:
                self.input_tokens_sum += tokens.get("input", 0)
                self.output_tokens_sum += tokens.get("output", 0)
                self.total_tokens_sum += tokens.get("total", 0)
                self.token_record_count += 1

    def record_timeout(self):
        with self._lock:
            self.timeout_count += 1
            self.error_count += 1

    def record_retry(self):
        with self._lock:
            self.retry_count += 1

    def record_client_reset(self):
        with self._lock:
            self.client_reset_count += 1

    def snapshot(self):
        with self._lock:
            avg = self.latency_sum / self.latency_count if self.latency_count else 0.0
            queue = self.queue_wait_sum / self.queue_wait_count if self.queue_wait_count else 0.0
            return {
                "request_count": self.request_count,
                "error_count": self.error_count,
                "timeout_count": self.timeout_count,
                "retry_count": self.retry_count,
                "client_reset_count": self.client_reset_count,
                "slow_request_count": self.slow_request_count,
                "slow_threshold_s": self.slow_threshold,
                "avg_latency_ms": round(avg * 1000, 2),
                "avg_queue_wait_ms": round(queue * 1000, 2),
                "status_counts": dict(self.status_counts),
                "model_counts": dict(self.model_counts),
                "tokens": {
                    "input_total": self.input_tokens_sum,
                    "output_total": self.output_tokens_sum,
                    "total": self.total_tokens_sum,
                    "recorded_requests": self.token_record_count,
                },
            }
