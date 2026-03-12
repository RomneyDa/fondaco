"""Shared claim/redeem logic for resolved Polymarket positions.

Used by both app/claim.py (main trading app) and claim-server/claim.py
(standalone Fly.io deployment). All HTTP for the Builder Relayer goes
through curl_cffi to bypass Cloudflare WAF.

fetch_redeemable_positions() accepts an optional http_session parameter:
  - app/ passes a LoggingSession
  - claim-server/ and default use plain requests.Session()
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

# Polygon mainnet chain ID
POLYGON_CHAIN_ID = 137

# Conditional Token Framework contract (holds ERC-1155 outcome tokens).
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# USDC.e on Polygon (collateral token for all Polymarket markets).
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Builder Relayer endpoint for gasless Safe transactions.
BUILDER_RELAYER_URL = "https://relayer-v2.polymarket.com"

# Polymarket data API for querying positions.
DATA_API_BASE = "https://data-api.polymarket.com"

# Retry config for Cloudflare 403 blocks
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds, doubled each retry


def has_builder_creds():
    # type: () -> bool
    """Check if Builder API credentials are configured in .env."""
    return all([
        os.environ.get("POLYMARKET_BUILDER_API_KEY"),
        os.environ.get("POLYMARKET_BUILDER_SECRET"),
        os.environ.get("POLYMARKET_BUILDER_PASSPHRASE"),
    ])


def _get_builder_config():
    """Build a BuilderConfig from env vars."""
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    return BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=os.environ.get("POLYMARKET_BUILDER_API_KEY", ""),
            secret=os.environ.get("POLYMARKET_BUILDER_SECRET", ""),
            passphrase=os.environ.get("POLYMARKET_BUILDER_PASSPHRASE", ""),
        )
    )


def _get_relay_client(private_key):
    """Build a RelayClient (used for signer/config, not for HTTP)."""
    from py_builder_relayer_client.client import RelayClient

    return RelayClient(
        BUILDER_RELAYER_URL,
        POLYGON_CHAIN_ID,
        private_key,
        _get_builder_config(),
    )


def _get_curl_session():
    """Get a persistent curl_cffi session with Chrome impersonation.

    Warms up with a GET to establish Cloudflare clearance cookies before
    any POST requests are made.
    """
    global _curl_session
    if _curl_session is None:
        _curl_session = curl_requests.Session(impersonate="chrome142")
        _curl_session.get(BUILDER_RELAYER_URL + "/")
    return _curl_session


_curl_session = None  # type: Optional[curl_requests.Session]


def _reset_curl_session():
    """Force a fresh session on next use (e.g. after repeated 403s)."""
    global _curl_session
    if _curl_session is not None:
        _curl_session.close()
    _curl_session = None


def _curl_post(url, headers, body_json):
    """POST JSON via curl_cffi session with Chrome impersonation and retry on CF 403.

    body_json must be a pre-serialized JSON string (compact, no spaces).
    """
    session = _get_curl_session()
    delay = RETRY_DELAY
    for attempt in range(MAX_RETRIES):
        resp = session.post(
            url,
            headers=dict(headers, **{"Content-Type": "application/json"}),
            data=body_json,
        )
        if resp.status_code != 403:
            return resp
        logger.warning(
            "Cloudflare 403 on attempt %d/%d, retrying in %ds...",
            attempt + 1, MAX_RETRIES, delay,
        )
        _reset_curl_session()
        session = _get_curl_session()
        time.sleep(delay)
        delay *= 2
    return resp


def _curl_get(url):
    """GET via curl_cffi session with Chrome impersonation."""
    return _get_curl_session().get(url)


def _encode_redeem_positions(condition_id):
    # type: (str) -> str
    """ABI-encode a redeemPositions(address,bytes32,bytes32,uint256[]) call."""
    from eth_abi import encode
    from eth_utils import keccak, to_checksum_address

    selector = keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    parent_collection_id = b"\x00" * 32
    cond_bytes = bytes.fromhex(condition_id.replace("0x", ""))
    encoded_args = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            to_checksum_address(USDC_E),
            parent_collection_id,
            cond_bytes,
            [1, 2],
        ],
    )
    return "0x" + (selector + encoded_args).hex()


def fetch_redeemable_positions(safe_address, http_session=None):
    # type: (str, Optional[requests.Session]) -> List[Dict[str, Any]]
    """Query the data API for redeemable positions. Returns [] on any error.

    If http_session is None, creates a plain requests.Session(). Callers
    can pass a LoggingSession or any requests-compatible session.
    """
    close_session = http_session is None
    if http_session is None:
        http_session = requests.Session()
    try:
        resp = http_session.get(
            "{}/positions".format(DATA_API_BASE),
            params={
                "user": safe_address,
                "redeemable": "true",
                "sizeThreshold": "0",
                "limit": "500",
            },
            timeout=10,
        )
        resp.raise_for_status()
        positions = resp.json()
        if not isinstance(positions, list):
            return []
        return positions
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning("Failed to fetch redeemable positions: %s", e)
        return []
    finally:
        if close_session:
            http_session.close()


def _submit_transaction(builder_config, txn_request):
    """Submit a transaction directly via curl_cffi, bypassing the SDK's HTTP."""
    body_json = json.dumps(txn_request, separators=(",", ":"))
    headers = builder_config.generate_builder_headers(
        "POST", "/submit", body_json,
    ).to_dict()

    resp = _curl_post(
        "{}/submit".format(BUILDER_RELAYER_URL),
        headers,
        body_json,
    )
    if resp.status_code != 200:
        raise Exception(
            "Submit failed [status={}]: {}".format(
                resp.status_code, resp.text[:200],
            )
        )
    return resp.json()


def _poll_transaction(transaction_id, max_polls=10):
    """Poll until transaction is confirmed, failed, or timeout."""
    for _ in range(max_polls):
        try:
            resp = _curl_get(
                "{}/transaction?id={}".format(BUILDER_RELAYER_URL, transaction_id),
            )
            if resp.status_code == 200:
                txns = resp.json()
                if txns:
                    state = txns[0].get("state", "")
                    if state in ("STATE_MINED", "STATE_CONFIRMED"):
                        return True
                    if state == "STATE_FAILED":
                        return False
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(2)
    return False


def _redeem_positions(private_key, condition_ids, wait_for_confirm=True):
    # type: (str, List[str], bool) -> tuple
    """Submit redeemPositions txns via Builder Relayer.

    Bypasses the SDK's HTTP layer to use curl_cffi directly for the
    POST /submit call, avoiding Cloudflare WAF blocks.

    Returns (succeeded, failed) counts.
    """
    from py_builder_relayer_client.models import (
        SafeTransaction, SafeTransactionArgs, OperationType, TransactionType,
    )
    from py_builder_relayer_client.builder.safe import build_safe_transaction_request
    from eth_utils import to_checksum_address

    relay_client = _get_relay_client(private_key)
    builder_config = _get_builder_config()

    # Pre-fetch safe address and verify deployment once
    safe_address = relay_client.get_expected_safe()
    deployed_resp = _curl_get(
        "{}/deployed?address={}".format(BUILDER_RELAYER_URL, safe_address),
    )
    if deployed_resp.status_code != 200:
        raise Exception("Failed to check safe deployment")
    if not deployed_resp.json().get("deployed"):
        raise Exception("Safe {} is not deployed".format(safe_address))

    from_address = relay_client.signer.address()

    succeeded = 0
    failed = 0
    for cond_id in condition_ids:
        try:
            # Get fresh nonce for each transaction
            nonce_resp = _curl_get(
                "{}/nonce?address={}&type={}".format(
                    BUILDER_RELAYER_URL, from_address, TransactionType.SAFE.value,
                ),
            )
            if nonce_resp.status_code != 200:
                raise Exception("Failed to get nonce")
            nonce = nonce_resp.json().get("nonce")

            calldata = _encode_redeem_positions(cond_id)
            txn = SafeTransaction(
                to=to_checksum_address(CTF_CONTRACT),
                operation=OperationType.Call,
                data=calldata,
                value="0",
            )

            safe_args = SafeTransactionArgs(
                from_address=from_address,
                nonce=nonce,
                chain_id=POLYGON_CHAIN_ID,
                transactions=[txn],
            )
            txn_request = build_safe_transaction_request(
                signer=relay_client.signer,
                args=safe_args,
                config=relay_client.contract_config,
                metadata="redeemPositions({})".format(cond_id),
            ).to_dict()

            logger.info("Redeeming condition %s ...", cond_id)
            result = _submit_transaction(builder_config, txn_request)
            tx_id = result.get("transactionID", "")
            tx_hash = result.get("transactionHash", "")
            logger.info("Submitted: tx_id=%s tx_hash=%s", tx_id, tx_hash)

            if wait_for_confirm:
                confirmed = _poll_transaction(tx_id)
                if confirmed:
                    logger.info("Confirmed: %s", cond_id)
                    succeeded += 1
                else:
                    logger.error("Transaction failed or timed out: %s", cond_id)
                    failed += 1
            else:
                succeeded += 1
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            logger.error("Redeem failed for %s: %s", cond_id, e)
            failed += 1

    return succeeded, failed


def claim_redeemable(private_key, safe_address, wait_for_confirm=True, http_session=None):
    # type: (str, str, bool, Optional[requests.Session]) -> tuple
    """Claim all redeemable positions. Returns (succeeded, failed, total_value).

    Safe to call from anywhere — catches all exceptions and returns (0, 0, 0)
    on setup failures.
    """
    try:
        positions = fetch_redeemable_positions(safe_address, http_session=http_session)
        # Filter to winning positions only (currentValue > 0)
        positions = [p for p in positions if p.get("currentValue", 0) > 0]
        if not positions:
            return (0, 0, 0.0)

        total_value = sum(pos.get("currentValue", 0) for pos in positions)
        condition_ids = list({pos["conditionId"] for pos in positions})

        logger.info(
            "Found %d redeemable position(s) (~$%.2f) across %d condition(s)",
            len(positions), total_value, len(condition_ids),
        )

        succeeded, failed = _redeem_positions(
            private_key, condition_ids, wait_for_confirm=wait_for_confirm,
        )
        return (succeeded, failed, total_value)
    except (requests.RequestException, OSError, ValueError, KeyError, RuntimeError) as e:
        logger.error("Claim error: %s", e)
        return (0, 0, 0.0)
