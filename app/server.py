"""HTTP control server for the multi-market app.

Runs a stdlib HTTPServer in a daemon thread, exposing endpoints to
query status, manage markets, toggle trading, and trigger shutdown.
"""

import json
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.app_manager import AppManager

logger = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    """Request handler for the control server."""

    app_manager = None  # type: Optional[AppManager]

    def log_message(self, format, *args):
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

    def _require_app_manager(self):
        # type: () -> Optional[AppManager]
        if self.app_manager is None:
            self._send_json({"error": "No app manager"}, 500)
            return None
        return self.app_manager

    def _require_slug(self, body):
        # type: (Dict[str, Any]) -> Optional[str]
        """Extract and validate slug_prefix from request body."""
        slug_prefix = body.get("slug_prefix")
        if not slug_prefix or not isinstance(slug_prefix, str):
            self._send_json({"error": "Missing 'slug_prefix'"}, 400)
            return None
        return slug_prefix

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/control/status":
            self._handle_status()
        elif self.path == "/api/control/optimizer-status":
            self._handle_optimizer_status()
        elif self.path == "/api/control/optimizer-events":
            self._handle_optimizer_events()
        elif self.path == "/api/control/optimizer-config":
            self._handle_optimizer_config_get()
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        routes = {
            "/api/control/market-on": self._handle_market_on,
            "/api/control/market-off": self._handle_market_off,
            "/api/control/trade-on": self._handle_trade_on,
            "/api/control/trade-off": self._handle_trade_off,
            "/api/control/set-cash": self._handle_set_cash,
            "/api/control/reload": self._handle_reload,
            "/api/control/set-auto-exit": self._handle_set_auto_exit,
            "/api/control/kill": self._handle_kill,
            "/api/control/stop": self._handle_stop,
            "/api/control/optimizer-on": self._handle_optimizer_on,
            "/api/control/optimizer-off": self._handle_optimizer_off,
            "/api/control/optimizer-config": self._handle_optimizer_config,
        }
        handler = routes.get(self.path)
        if handler:
            handler()
        else:
            self._send_json({"error": "Not found"}, 404)

    # --- App-level endpoints ---

    def _handle_status(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        self._send_json(mgr.get_status())

    def _handle_stop(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        mgr.request_stop()
        self._send_json({"ok": True, "stopping": True})

    def _handle_kill(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        mgr.request_stop_after_session()
        self._send_json({"ok": True, "stop_after_session": True})

    # --- Market lifecycle ---

    def _handle_market_on(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            body = self._read_json_body()
            slug_prefix = self._require_slug(body)
            if slug_prefix is None:
                return
            window = body.get("window")
            market_type = body.get("market_type", "updown")
            restore = body.get("restore", False)

            kwargs = {"market_type": market_type}
            if window is not None:
                kwargs["window_duration"] = int(window)

            runner = mgr.start_market(slug_prefix, **kwargs)

            if restore:
                runner.restore_strategies()

            self._send_json({
                "ok": True,
                "slug_prefix": slug_prefix,
                "slug_key": runner.slug_key,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_market_off(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            body = self._read_json_body()
            slug_prefix = self._require_slug(body)
            if slug_prefix is None:
                return
            after_session = body.get("after_session", False)
            mgr.stop_market(slug_prefix, after_session=after_session)
            self._send_json({"ok": True, "slug_prefix": slug_prefix})
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    # --- Bot lifecycle (per-market) ---

    def _handle_trade_on(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            body = self._read_json_body()
            slug_prefix = self._require_slug(body)
            if slug_prefix is None:
                return
            strategy_name = body.get("strategy")
            if not strategy_name:
                self._send_json({"error": "Missing 'strategy' field"}, 400)
                return
            params = body.get("params", {})
            cash = body.get("cash")
            dry_run = body.get("dry_run", False)
            per_session = body.get("per_session", False)
            session_cap = body.get("session_cap")
            if session_cap is not None:
                session_cap = float(session_cap)
            session_cash_ratio = body.get("session_cash_ratio")

            runner = mgr.get_market(slug_prefix)
            runner.enable_trading(
                strategy_name, params, cash=cash, dry_run=dry_run,
                per_session=per_session, session_cap=session_cap,
                session_cash_ratio=session_cash_ratio,
            )
            self._send_json({
                "ok": True,
                "trading": True,
                "strategy": strategy_name,
                "slug_prefix": slug_prefix,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_trade_off(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            body = self._read_json_body()
            slug_prefix = self._require_slug(body)
            if slug_prefix is None:
                return
            strategy_name = body.get("strategy")
            runner = mgr.get_market(slug_prefix)
            runner.disable_trading(strategy_name=strategy_name)
            self._send_json({
                "ok": True,
                "trading": False,
                "strategy": strategy_name,
                "slug_prefix": slug_prefix,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_set_cash(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            body = self._read_json_body()
            slug_prefix = self._require_slug(body)
            if slug_prefix is None:
                return
            cash = body.get("cash")
            if cash is None:
                self._send_json({"error": "Missing 'cash' field"}, 400)
                return
            strategy_name = body.get("strategy")
            session_cash_ratio = body.get("session_cash_ratio")
            session_cap = body.get("session_cap")
            if session_cap is not None:
                session_cap = float(session_cap)
            clear_session_limit = body.get("clear_session_limit", False)
            runner = mgr.get_market(slug_prefix)
            runner.set_cash(
                float(cash), strategy_name=strategy_name,
                session_cash_ratio=session_cash_ratio,
                session_cap=session_cap,
                clear_session_limit=clear_session_limit,
            )
            self._send_json({
                "ok": True,
                "cash": round(float(cash), 4),
                "slug_prefix": slug_prefix,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_set_auto_exit(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            body = self._read_json_body()
            slug_prefix = self._require_slug(body)
            if slug_prefix is None:
                return
            enabled = body.get("enabled")
            if enabled is None:
                self._send_json({"error": "Missing 'enabled' field"}, 400)
                return
            strategy_name = body.get("strategy")
            runner = mgr.get_market(slug_prefix)
            runner.set_auto_exit(bool(enabled), strategy_name=strategy_name)
            self._send_json({
                "ok": True,
                "auto_exit": bool(enabled),
                "slug_prefix": slug_prefix,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_reload(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            body = self._read_json_body()
            slug_prefix = self._require_slug(body)
            if slug_prefix is None:
                return
            strategy_name = body.get("strategy")
            if not strategy_name:
                self._send_json({"error": "Missing 'strategy' field"}, 400)
                return

            runner = mgr.get_market(slug_prefix)

            # Find the worker to get its config
            with runner._lock:
                target = None
                for w in runner._workers:
                    if w.strategy_name == strategy_name:
                        target = w
                        break
            if target is None:
                self._send_json(
                    {"error": "No active worker for '{}'".format(strategy_name)}, 404,
                )
                return

            state = runner._read_worker_state(strategy_name)
            cash = state.get("cash", target.cash) if state else target.cash

            runner.enable_trading(
                strategy_name, target.params, cash=cash,
                dry_run=target.dry_run, per_session=target.per_session,
                session_cap=target.session_cap,
                session_cash_ratio=target.session_cash_ratio,
            )
            self._send_json({
                "ok": True,
                "strategy": strategy_name,
                "reloaded": True,
                "slug_prefix": slug_prefix,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 400)


    # --- Optimizer endpoints ---

    def _handle_optimizer_on(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            body = self._read_json_body()
            interval = body.get("interval", 30)
            lookback = body.get("lookback", 30)
            min_sharpe = body.get("min_sharpe", 0.2)
            starting_cash = body.get("starting_cash", 100.0)
            max_edge_iters = body.get("max_edge_iters", 2)
            result = mgr.start_optimizer(
                interval=int(interval),
                lookback=int(lookback),
                min_sharpe=float(min_sharpe),
                starting_cash=float(starting_cash),
                max_edge_iters=int(max_edge_iters),
            )
            status = 200 if result.get("ok") else 400
            self._send_json(result, status)
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_optimizer_off(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            result = mgr.stop_optimizer()
            status = 200 if result.get("ok") else 400
            self._send_json(result, status)
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_optimizer_status(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            self._send_json(mgr.get_optimizer_status())
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_optimizer_config_get(self):
        """GET handler: return saved config (no app manager required)."""
        try:
            from core.defaults import OPTIMIZER_DIR
            config_file = os.path.join(OPTIMIZER_DIR, "config.json")
            try:
                with open(config_file, "r") as f:
                    config = json.load(f)
            except (OSError, json.JSONDecodeError):
                config = {}
            self._send_json({"ok": True, "config": config})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_optimizer_config(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            body = self._read_json_body()
            from core.defaults import OPTIMIZER_DIR
            config_file = os.path.join(OPTIMIZER_DIR, "config.json")
            # Read existing
            try:
                with open(config_file, "r") as f:
                    config = json.load(f)
            except (OSError, json.JSONDecodeError):
                config = {}
            # Update with provided fields
            for key in ("interval_minutes", "lookback", "min_sharpe", "starting_cash"):
                if key in body:
                    config[key] = body[key]
            # Save
            os.makedirs(OPTIMIZER_DIR, exist_ok=True)
            tmp = config_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp, config_file)
            self._send_json({"ok": True, "config": config})
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_optimizer_events(self):
        mgr = self._require_app_manager()
        if mgr is None:
            return
        try:
            from core.defaults import OPTIMIZER_DIR
            events_file = os.path.join(OPTIMIZER_DIR, "events.jsonl")
            events = []
            if os.path.exists(events_file):
                with open(events_file, "r") as f:
                    lines = f.readlines()
                # Return last 100 events, newest first
                for line in reversed(lines[-100:]):
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            self._send_json({"events": events})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)


class ControlServer:
    """HTTP control server wrapping HTTPServer in a daemon thread."""

    def __init__(self, app_manager, port):
        # type: (AppManager, int) -> None
        self.port = port
        _Handler.app_manager = app_manager
        self._server = HTTPServer(("0.0.0.0", port), _Handler)
        self._thread = None  # type: Optional[threading.Thread]

    def start(self):
        # type: () -> None
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="control-server",
        )
        self._thread.start()
        logger.info("Control server listening on port %d", self.port)

    def stop(self):
        # type: () -> None
        self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("Control server stopped.")
