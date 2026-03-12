"""CLOB authentication helpers: Safe address derivation, API creds, auth cache.

Extracted from bot/__main__.py. Used by Runner and CLI commands for
authenticating with Polymarket's CLOB API.
"""

import json
import logging
import os
from typing import Any, Dict, Optional

from core.auth_shared import (
    POLYGON_CHAIN_ID,
    derive_safe_address as _derive_safe_address_from_key,
)
from core.defaults import BASE_DIR, CLOB_API_BASE

logger = logging.getLogger(__name__)

# Polymarket signature types: 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE
# Browser-wallet users (MetaMask) get a Gnosis Safe proxy on Polygon where
# deposits from polymarket.com live.  We use signature_type=2 so the CLOB
# validates the signature against the Safe, and pass the Safe address as
# the 'funder' so orders draw from that balance.
POLY_GNOSIS_SAFE = 2

AUTH_CACHE_FILE = os.path.join(BASE_DIR, ".cache", "auth.json")


# ---------------------------------------------------------------------------
# Auth cache
# ---------------------------------------------------------------------------


def _load_auth_cache(eoa_address):
    # type: (str) -> Optional[Dict[str, Any]]
    """Load cached auth data if it matches the current EOA."""
    try:
        with open(AUTH_CACHE_FILE, "r") as f:
            data = json.load(f)
        if data.get("eoa_address", "").lower() == eoa_address.lower():
            return data
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def _save_auth_cache(data):
    # type: (Dict[str, Any]) -> None
    """Write auth cache to disk."""
    os.makedirs(os.path.dirname(AUTH_CACHE_FILE), exist_ok=True)
    with open(AUTH_CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _clear_auth_cache():
    # type: () -> None
    """Delete the auth cache file."""
    try:
        os.remove(AUTH_CACHE_FILE)
        logger.info("Auth cache cleared.")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main auth entry point
# ---------------------------------------------------------------------------


def get_auth(private_key, force_refresh=False):
    # type: (str, bool) -> Dict[str, Any]
    """Get Safe address and API creds, using cache when possible.

    On first run: derives Safe address via RPC, creates API creds via CLOB,
    and caches both to .cache/auth.json.
    On subsequent runs: loads from cache (instant, no network calls).
    Set force_refresh=True to ignore cache and re-derive everything.
    """
    from eth_account import Account
    from py_clob_client.client import ClobClient

    eoa_address = Account.from_key(private_key).address

    if not force_refresh:
        cached = _load_auth_cache(eoa_address)
        if cached and all(k in cached for k in ("safe_address", "api_key", "api_secret", "api_passphrase")):
            logger.info("Auth loaded from cache (.cache/auth.json)")
            return cached

    # Derive Safe address
    logger.info("Deriving Safe address via Polygon RPC...")
    safe_address = _derive_safe_address_from_key(private_key)
    logger.info("Safe wallet: %s", safe_address)

    # Create API creds
    logger.info("Creating API credentials...")
    client = ClobClient(
        CLOB_API_BASE,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        signature_type=POLY_GNOSIS_SAFE,
        funder=safe_address,
    )
    creds = client.create_or_derive_api_creds()

    # Cache everything
    data = {
        "eoa_address": eoa_address,
        "safe_address": safe_address,
        "api_key": creds.api_key,
        "api_secret": creds.api_secret,
        "api_passphrase": creds.api_passphrase,
    }
    _save_auth_cache(data)
    logger.info("Auth cached to .cache/auth.json")
    return data


def build_clob_client(private_key, auth):
    # type: (str, Dict[str, Any]) -> Any
    """Build and return a ready-to-use ClobClient from auth data."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    client = ClobClient(
        CLOB_API_BASE,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        signature_type=POLY_GNOSIS_SAFE,
        funder=auth["safe_address"],
    )
    client.set_api_creds(ApiCreds(
        api_key=auth["api_key"],
        api_secret=auth["api_secret"],
        api_passphrase=auth["api_passphrase"],
    ))
    return client
