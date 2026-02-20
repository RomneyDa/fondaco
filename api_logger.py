"""Transparent HTTP request/response logger with rate-limit handling.

Every API call made through LoggingSession is recorded as a JSONL entry
under data/api_raw/{host}/{YYYY-MM-DD}.jsonl.  The structure is intentionally
generic so the same files work for any Polymarket market type (5-min, 1-min,
different assets, etc.).

Rate-limited responses (HTTP 429) are automatically retried with exponential
backoff, respecting Retry-After headers when present.
"""

import json
import os
import time
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests

from defaults import API_RAW_DIR

logger = logging.getLogger(__name__)

MAX_RETRIES = 4
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0


class LoggingSession(requests.Session):
    """A requests.Session that logs every request/response and retries on 429."""

    def request(self, method, url, **kwargs):
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
                _write_entry(host, ts, entry)

                # Handle 429 rate limit
                if resp.status_code == 429 and attempt <= MAX_RETRIES:
                    retry_after = _parse_retry_after(resp)
                    wait = retry_after if retry_after is not None else backoff
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
    """Append one log entry to the appropriate JSONL file."""
    date_str = ts.strftime("%Y-%m-%d")
    host_dir = os.path.join(API_RAW_DIR, host)
    os.makedirs(host_dir, exist_ok=True)
    filepath = os.path.join(host_dir, f"{date_str}.jsonl")

    try:
        with open(filepath, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.warning(f"Failed to write API log entry: {e}")
