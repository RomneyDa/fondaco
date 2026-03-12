"""Claim server — periodically redeems resolved Polymarket positions.

Runs as a long-lived process. Checks for redeemable positions every
CLAIM_INTERVAL seconds (default 300 = 5 minutes) and redeems them
via the Builder Relayer.

Also serves a tiny web UI on port 8080 with a manual CLAIM button.

Required env vars:
    POLYMARKET_PRIVATE_KEY
    POLYMARKET_BUILDER_API_KEY
    POLYMARKET_BUILDER_SECRET
    POLYMARKET_BUILDER_PASSPHRASE

Optional env vars:
    CLAIM_INTERVAL  — seconds between claim cycles (default: 300)
    UI_PORT         — HTTP port for the web UI (default: 8080)
"""

import json
import logging
import os
import random
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

# Add project root to sys.path so claim-server wrappers can import from core/
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s — %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claim-server.log")

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)
logger = logging.getLogger("claim-server")

# Shared state for the claim function — set in main(), read by the handler.
_claim_ctx = {
    "private_key": None,
    "safe_address": None,
    "lock": threading.Lock(),
    "running": False,
    "last_result": None,
}

HTML_PAGE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claim Server</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 400px; margin: 80px auto; text-align: center; }
  button { font-size: 1.5rem; padding: 16px 48px; cursor: pointer; border: none; border-radius: 8px;
           background: #2563eb; color: #fff; }
  button:hover { background: #1d4ed8; }
  button:disabled { background: #94a3b8; cursor: not-allowed; }
  #status { margin-top: 24px; font-size: 1rem; color: #475569; min-height: 1.5em; }
</style>
</head>
<body>
  <h1>Polymarket Claim</h1>
  <button id="btn" onclick="doClaim()">CLAIM</button>
  <div id="status"></div>
<script>
async function doClaim() {
  const btn = document.getElementById('btn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = 'Claiming...';
  try {
    const r = await fetch('/claim', { method: 'POST' });
    const d = await r.json();
    if (d.error) {
      status.textContent = 'Error: ' + d.error;
    } else {
      status.textContent = d.succeeded + ' claimed, ' + d.failed + ' failed (~$' + d.total_value.toFixed(2) + ')';
    }
  } catch (e) {
    status.textContent = 'Request failed: ' + e;
  }
  btn.disabled = false;
}
</script>
</body>
</html>
"""


class ClaimHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode())

    def do_POST(self):
        if self.path != "/claim":
            self.send_response(404)
            self.end_headers()
            return

        ctx = _claim_ctx
        if not ctx["lock"].acquire(blocking=False):
            self._json_response(409, {"error": "Claim already in progress"})
            return

        try:
            from claim import claim_redeemable
            succeeded, failed, total_value = claim_redeemable(
                ctx["private_key"], ctx["safe_address"], wait_for_confirm=True,
            )
            result = {"succeeded": succeeded, "failed": failed, "total_value": total_value}
            ctx["last_result"] = result
            self._json_response(200, result)
        except Exception as e:
            logger.exception("Manual claim failed")
            self._json_response(500, {"error": str(e)})
        finally:
            ctx["lock"].release()

    def _json_response(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info("HTTP %s", fmt % args)


def main():
    # Load .env if present (optional — env vars can come from the platform)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        logger.error("POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)

    from claim import has_builder_creds
    if not has_builder_creds():
        logger.error(
            "Missing Builder API credentials. Set POLYMARKET_BUILDER_API_KEY, "
            "POLYMARKET_BUILDER_SECRET, and POLYMARKET_BUILDER_PASSPHRASE."
        )
        sys.exit(1)

    interval = int(os.environ.get("CLAIM_INTERVAL", "900"))
    ui_port = int(os.environ.get("UI_PORT", "8080"))

    # Derive Safe address once at startup
    from auth import derive_safe_address
    safe_address = derive_safe_address(private_key)
    logger.info("Safe address: %s", safe_address)
    logger.info("Claim interval: %ds", interval)

    _claim_ctx["private_key"] = private_key
    _claim_ctx["safe_address"] = safe_address

    # Start web UI in a daemon thread
    server = HTTPServer(("0.0.0.0", ui_port), ClaimHandler)
    ui_thread = threading.Thread(target=server.serve_forever, daemon=True)
    ui_thread.start()
    logger.info("UI running on http://0.0.0.0:%d", ui_port)

    from claim import claim_redeemable

    while True:
        try:
            succeeded, failed, total_value = claim_redeemable(
                private_key, safe_address, wait_for_confirm=True,
            )
            if succeeded > 0 or failed > 0:
                logger.info(
                    "Cycle complete: %d claimed, %d failed (~$%.2f)",
                    succeeded, failed, total_value,
                )
            else:
                logger.info("No redeemable positions")
        except Exception:
            logger.exception("Claim cycle failed")

        jitter = random.uniform(-0.25, 0.25) * interval
        time.sleep(interval + jitter)


if __name__ == "__main__":
    main()
