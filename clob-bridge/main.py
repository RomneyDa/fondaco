"""Thin CLOB bridge HTTP server.

Wraps py_clob_client as a JSON API for the Rust bot worker.
Handles auth, order creation/signing, posting, cancellation, and balance queries.

Usage:
    cd clob-bridge
    uv sync
    uv run python main.py [--port 8421]
"""

import json
import logging
import os
import sys
import time

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Auth & client setup
# ---------------------------------------------------------------------------

_client = None
_private_key = None


def _get_client():
    global _client, _private_key
    if _client is not None:
        return _client

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    _private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not _private_key:
        logger.error("POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)

    clob_url = "https://clob.polymarket.com"
    chain_id = 137
    sig_type = 2  # POLY_GNOSIS_SAFE

    # Load or derive auth
    cache_file = os.path.join(os.path.dirname(__file__), "..", ".cache", "auth.json")
    auth = None
    try:
        with open(cache_file) as f:
            auth = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass

    if auth and all(k in auth for k in ("safe_address", "api_key", "api_secret", "api_passphrase")):
        safe_address = auth["safe_address"]
        client = ClobClient(
            clob_url, key=_private_key, chain_id=chain_id,
            signature_type=sig_type, funder=safe_address,
        )
        client.set_api_creds(ApiCreds(
            api_key=auth["api_key"],
            api_secret=auth["api_secret"],
            api_passphrase=auth["api_passphrase"],
        ))
        logger.info("CLOB client initialized from cached auth (safe=%s)", safe_address[:12])
    else:
        # Derive fresh
        from eth_account import Account
        from eth_abi import encode
        from eth_utils import keccak
        import requests as req

        eoa = Account.from_key(_private_key).address
        fn_sel = keccak(b"getSafeAddress(address)")[:4]
        calldata = "0x" + fn_sel.hex() + encode(["address"], [eoa]).hex()
        rpc_urls = ["https://polygon-bor-rpc.publicnode.com", "https://polygon.llamarpc.com"]
        safe_address = None
        for rpc in rpc_urls:
            try:
                resp = req.post(rpc, json={"jsonrpc": "2.0", "method": "eth_call",
                                           "params": [{"to": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
                                                       "data": calldata}, "latest"], "id": 1}, timeout=10)
                result = resp.json()
                if "result" in result and len(result["result"]) >= 42:
                    from eth_abi import decode
                    safe_address = decode(["address"], bytes.fromhex(result["result"][2:]))[0]
                    break
            except Exception:
                continue
        if not safe_address:
            logger.error("Failed to derive Safe address")
            sys.exit(1)

        client = ClobClient(clob_url, key=_private_key, chain_id=chain_id,
                            signature_type=sig_type, funder=safe_address)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump({"eoa_address": eoa, "safe_address": safe_address,
                       "api_key": creds.api_key, "api_secret": creds.api_secret,
                       "api_passphrase": creds.api_passphrase}, f, indent=2)
        logger.info("CLOB client initialized (fresh auth, safe=%s)", safe_address[:12])

    _client = client
    return client


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/create_and_post_order", methods=["POST"])
def create_and_post_order():
    """Create, sign, and post an order. Returns order ID and fill amounts."""
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

    data = request.json
    client = _get_client()

    try:
        order_args_kwargs = {
            "token_id": data["token_id"],
            "price": float(data["price"]),
            "size": float(data["size"]),
            "side": data["side"],
        }
        if data.get("expiration"):
            order_args_kwargs["expiration"] = data["expiration"]

        order_args = OrderArgs(**order_args_kwargs)
        options = PartialCreateOrderOptions(
            tick_size=data.get("tick_size", "0.01"),
            neg_risk=data.get("neg_risk", False),
        )

        signed_order = client.create_order(order_args, options)

        order_type_str = data.get("order_type", "FAK")
        order_type = {"FAK": OrderType.FAK, "GTD": OrderType.GTD, "GTC": OrderType.GTC}.get(
            order_type_str, OrderType.FAK
        )

        resp = client.post_order(signed_order, orderType=order_type)
        return jsonify({"success": True, "response": resp})

    except Exception as e:
        logger.error("Order failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/get_order", methods=["GET"])
def get_order():
    """Check order status."""
    client = _get_client()
    order_id = request.args.get("order_id")
    try:
        resp = client.get_order(order_id)
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/cancel_order", methods=["POST"])
def cancel_order():
    """Cancel a single order."""
    client = _get_client()
    data = request.json
    try:
        client.cancel(data["order_id"])
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/cancel_orders", methods=["POST"])
def cancel_orders():
    """Cancel multiple orders."""
    client = _get_client()
    data = request.json
    try:
        client.cancel_orders(data["order_ids"])
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/balance", methods=["GET"])
def get_balance():
    """Get token balance/allowance."""
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    client = _get_client()
    token_id = request.args.get("token_id")
    try:
        client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        ba = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        return jsonify(ba)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    # Eagerly init client
    _get_client()

    logger.info("CLOB bridge starting on port %d", args.port)
    app.run(host="127.0.0.1", port=args.port, threaded=True)
