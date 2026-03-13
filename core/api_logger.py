"""Transparent HTTP request/response logger with rate-limit handling.

Every API call made through LoggingSession is recorded as a JSONL entry
under data/api_raw/{host}/{YYYY-MM-DD}.jsonl.  The structure is intentionally
generic so the same files work for any Polymarket market type (5-min, 1-min,
different assets, etc.).

Rate-limited responses (HTTP 429) are automatically retried with exponential
backoff, respecting Retry-After headers when present.

Thread-safe: uses a lock for log file writes and configures connection pooling
for concurrent HTTP requests.
"""

import json
import os
import random
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from core.defaults import API_RAW_DIR

logger = logging.getLogger(__name__)

MAX_RETRIES = 4
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0

# Module-level lock for JSONL file writes
_write_lock = threading.Lock()

# Hosts that generate high-volume logs (GB/day). Only logged when verbose=True.
_QUIET_HOSTS = {"clob.polymarket.com", "gamma-api.polymarket.com"}


class LoggingSession(requests.Session):
    """A requests.Session that logs every request/response and retries on 429.

    Thread-safe for concurrent GET requests (no shared cookie/auth state).
    Connection pool sized for high-frequency parallel polling.
    """

    def __init__(self, default_timeout=15, verbose=False):
        super().__init__()
        self._default_timeout = default_timeout
        self._verbose = verbose
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def request(self, method, url, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = self._default_timeout
        parsed = urlparse(url)
        host = parsed.hostname or "unknown"
        path = parsed.path
        params = kwargs.get("params")

        backoff = INITIAL_BACKOFF_S

        for attempt in range(1, MAX_RETRIES + 2):  # 1 initial + MAX_RETRIES retries
            ts = datetime.now(timezone.utc)
            start = time.monotonic()

            entry = {
                "timestamp_utc": ts.isoformat(),
                "method": method.upper(),
                "url": url,
                "host": host,
                "path": path,
                "params": params,
                "attempt": attempt,
            }

            try:
                resp = super().request(method, url, **kwargs)
                elapsed_ms = round((time.monotonic() - start) * 1000, 1)

                entry["status_code"] = resp.status_code
                entry["elapsed_ms"] = elapsed_ms

                try:
                    entry["response_body"] = resp.json()
                except (ValueError, json.JSONDecodeError):
                    body_text = resp.text
                    if len(body_text) > 10_000:
                        body_text = body_text[:10_000] + "...[truncated]"
                    entry["response_body"] = body_text

                entry["error"] = None
                if self._verbose or host not in _QUIET_HOSTS:
                    _write_entry(host, ts, entry)

                # Handle 429 rate limit
                if resp.status_code == 429 and attempt <= MAX_RETRIES:
                    retry_after = _parse_retry_after(resp)
                    wait = retry_after if retry_after is not None else backoff
                    wait *= (0.5 + random.random())  # jitter: 50%-150% of base
                    logger.warning(
                        f"Rate limited (429) on {method} {path} "
                        f"(attempt {attempt}), retrying in {wait:.1f}s"
                    )
                    time.sleep(wait)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
                    continue

                return resp

            except Exception as e:
                elapsed_ms = round((time.monotonic() - start) * 1000, 1)
                entry["status_code"] = None
                entry["elapsed_ms"] = elapsed_ms
                entry["response_body"] = None
                entry["error"] = f"{type(e).__name__}: {e}"
                if self._verbose or host not in _QUIET_HOSTS:
                    _write_entry(host, ts, entry)
                raise

        # All retries exhausted on 429 — return the last response as-is
        return resp


def _parse_retry_after(resp: requests.Response) -> Optional[float]:
    """Parse the Retry-After header, returning seconds to wait or None."""
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _write_entry(host: str, ts: datetime, entry: dict):
    """Append one log entry to the appropriate JSONL file (thread-safe)."""
    date_str = ts.strftime("%Y-%m-%d")
    host_dir = os.path.join(API_RAW_DIR, host)
    os.makedirs(host_dir, exist_ok=True)
    filepath = os.path.join(host_dir, f"{date_str}.jsonl")

    line = json.dumps(entry, default=str) + "\n"
    try:
        with _write_lock:
            with open(filepath, "a") as f:
                f.write(line)
    except OSError as e:
        logger.warning(f"Failed to write API log entry: {e}")
