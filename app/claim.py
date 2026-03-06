"""Claim/redeem resolved Polymarket positions via Builder Relayer.

Extracted from bot/__main__.py. Provides both foreground (CLI) and
background (daemon thread) claim functionality.
"""

import logging
import os
import threading
from typing import Any, Dict, List, Optional

from core.api_logger import LoggingSession

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


def has_builder_creds():
    # type: () -> bool
    """Check if Builder API credentials are configured in .env."""
    return all([
        os.environ.get("POLYMARKET_BUILDER_API_KEY"),
        os.environ.get("POLYMARKET_BUILDER_SECRET"),
        os.environ.get("POLYMARKET_BUILDER_PASSPHRASE"),
    ])


def _get_builder_relay_client(private_key):
    """Build a RelayClient with Builder API credentials from .env.

    Raises ValueError if credentials are missing.
    """
    from py_builder_relayer_client.client import RelayClient
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    api_key = os.environ.get("POLYMARKET_BUILDER_API_KEY", "")
    secret = os.environ.get("POLYMARKET_BUILDER_SECRET", "")
    passphrase = os.environ.get("POLYMARKET_BUILDER_PASSPHRASE", "")

    if not all([api_key, secret, passphrase]):
        raise ValueError(
            "Builder API credentials not set. "
            "Set POLYMARKET_BUILDER_API_KEY, POLYMARKET_BUILDER_SECRET, and "
            "POLYMARKET_BUILDER_PASSPHRASE in .env. "
            "Generate them at https://polymarket.com/settings?tab=builder"
        )

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=api_key,
            secret=secret,
            passphrase=passphrase,
        )
    )

    return RelayClient(
        BUILDER_RELAYER_URL,
        POLYGON_CHAIN_ID,
        private_key,
        builder_config,
    )


def _encode_redeem_positions(condition_id):
    # type: (str) -> str
    """ABI-encode a redeemPositions(address,bytes32,bytes32,uint256[]) call."""
    from eth_abi import encode
    from eth_utils import keccak, to_checksum_address

    selector = keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    # parentCollectionId is always zero for top-level markets
    parent_collection_id = b"\x00" * 32
    # Condition ID: strip 0x prefix and convert to bytes
    cond_bytes = bytes.fromhex(condition_id.replace("0x", ""))
    # indexSets [1, 2] — redeem both outcome slots for binary markets
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
    # type: (str, Optional[LoggingSession]) -> List[Dict[str, Any]]
    """Query the data API for redeemable positions. Returns [] on any error."""
    close_session = http_session is None
    if http_session is None:
        http_session = LoggingSession()
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
    except Exception as e:
        logger.warning("Failed to fetch redeemable positions: %s", e)
        return []
    finally:
        if close_session:
            http_session.close()


def _redeem_positions(private_key, condition_ids, wait_for_confirm=True):
    # type: (str, List[str], bool) -> tuple
    """Submit redeemPositions txns via Builder Relayer.

    Returns (succeeded, failed) counts.
    """
    from py_builder_relayer_client.models import SafeTransaction, OperationType
    from eth_utils import to_checksum_address

    relay_client = _get_builder_relay_client(private_key)

    succeeded = 0
    failed = 0
    for cond_id in condition_ids:
        try:
            calldata = _encode_redeem_positions(cond_id)
            txn = SafeTransaction(
                to=to_checksum_address(CTF_CONTRACT),
                operation=OperationType.Call,
                data=calldata,
                value="0",
            )

            logger.info("Redeeming condition %s ...", cond_id)
            response = relay_client.execute(
                [txn], "redeemPositions({})".format(cond_id),
            )
            logger.info(
                "Submitted: tx_id=%s tx_hash=%s",
                response.transaction_id, response.transaction_hash,
            )

            if wait_for_confirm:
                result = response.wait()
                if result:
                    logger.info("Confirmed: %s", cond_id)
                    succeeded += 1
                else:
                    logger.error("Transaction failed or timed out: %s", cond_id)
                    failed += 1
            else:
                # Fire-and-forget: count as succeeded once submitted
                succeeded += 1
        except Exception as e:
            logger.error("Redeem failed for %s: %s", cond_id, e)
            failed += 1

    return succeeded, failed


def claim_redeemable(private_key, safe_address, wait_for_confirm=True):
    # type: (str, str, bool) -> tuple
    """Claim all redeemable positions. Returns (succeeded, failed, total_value).

    Safe to call from anywhere — catches all exceptions and returns (0, 0, 0)
    on setup failures.
    """
    try:
        positions = fetch_redeemable_positions(safe_address)
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
    except Exception as e:
        logger.error("Claim error: %s", e)
        return (0, 0, 0.0)


def claim_background(private_key, safe_address):
    # type: (str, str) -> None
    """Run claim_redeemable in a daemon thread (fire-and-forget, no waiting).

    Used by the runner after session resolution. Never blocks, never raises.
    """
    def _run():
        try:
            s, f, val = claim_redeemable(
                private_key, safe_address, wait_for_confirm=False,
            )
            if s > 0 or f > 0:
                logger.info(
                    "Background claim: %d submitted, %d failed (~$%.2f)",
                    s, f, val,
                )
        except Exception as e:
            logger.debug("Background claim thread error: %s", e)

    t = threading.Thread(target=_run, daemon=True, name="claim-redeemable")
    t.start()
