"""HTTP control server for the unified app process.

Runs a stdlib HTTPServer in a daemon thread, exposing endpoints to
query status, toggle trading, and trigger graceful shutdown.
"""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.runner import Runner

logger = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    """Request handler for the control server."""

    runner = None  # type: Optional[Runner]

    def log_message(self, format, *args):
        # Suppress default stderr logging
        pass

    def _set_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, data, status=200):
        # type: (Dict[str, Any], int) -> None
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        # type: () -> Dict[str, Any]
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/control/status":
            self._handle_status()
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/api/control/trade-on":
            self._handle_trade_on()
        elif self.path == "/api/control/trade-off":
            self._handle_trade_off()
        elif self.path == "/api/control/set-cash":
            self._handle_set_cash()
        elif self.path == "/api/control/stop":
            self._handle_stop()
        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_status(self):
        runner = self.runner
        if runner is None:
            self._send_json({"error": "No runner"}, 500)
            return
        self._send_json(runner.get_status())

    def _handle_trade_on(self):
        runner = self.runner
        if runner is None:
            self._send_json({"error": "No runner"}, 500)
            return
        try:
            body = self._read_json_body()
            strategy_name = body.get("strategy")
            if not strategy_name:
                self._send_json({"error": "Missing 'strategy' field"}, 400)
                return
            params = body.get("params", {})
            cash = body.get("cash")
            dry_run = body.get("dry_run", False)
            runner.enable_trading(strategy_name, params, cash=cash, dry_run=dry_run)
            self._send_json({"ok": True, "trading": True, "strategy": strategy_name})
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_trade_off(self):
        runner = self.runner
        if runner is None:
            self._send_json({"error": "No runner"}, 500)
            return
        try:
            body = self._read_json_body()
            strategy_name = body.get("strategy")
            runner.disable_trading(strategy_name=strategy_name)
            self._send_json({"ok": True, "trading": False, "strategy": strategy_name})
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_set_cash(self):
        runner = self.runner
        if runner is None:
            self._send_json({"error": "No runner"}, 500)
            return
        try:
            body = self._read_json_body()
            cash = body.get("cash")
            if cash is None:
                self._send_json({"error": "Missing 'cash' field"}, 400)
                return
            strategy_name = body.get("strategy")
            runner.set_cash(float(cash), strategy_name=strategy_name)
            self._send_json({"ok": True, "cash": round(float(cash), 4)})
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_stop(self):
        runner = self.runner
        if runner is None:
            self._send_json({"error": "No runner"}, 500)
            return
        runner.request_stop()
        self._send_json({"ok": True, "stopping": True})


class ControlServer:
    """HTTP control server wrapping HTTPServer in a daemon thread."""

    def __init__(self, runner, port):
        # type: (Runner, int) -> None
        self.port = port
        _Handler.runner = runner
        self._server = HTTPServer(("0.0.0.0", port), _Handler)
        self._thread = None  # type: Optional[threading.Thread]

    def start(self):
        # type: () -> None
        """Start serving in a daemon thread."""
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="control-server",
        )
        self._thread.start()
        logger.info("Control server listening on port %d", self.port)

    def stop(self):
        # type: () -> None
        """Shut down the server."""
        self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("Control server stopped.")
