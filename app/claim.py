"""Claim/redeem resolved Polymarket positions — app wrapper.

Delegates all shared logic to core.claim. This module adds:
  - claim_background() for fire-and-forget daemon-thread claiming
  - Default use of LoggingSession for HTTP in fetch_redeemable_positions
"""

import logging
import threading

from core.claim import (  # noqa: F401 — re-exported
    POLYGON_CHAIN_ID,
    CTF_CONTRACT,
    USDC_E,
    BUILDER_RELAYER_URL,
    DATA_API_BASE,
    MAX_RETRIES,
    RETRY_DELAY,
    has_builder_creds,
    _get_relay_client,
    fetch_redeemable_positions,
    claim_redeemable,
)

logger = logging.getLogger(__name__)


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
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            logger.debug("Background claim thread error: %s", e)

    t = threading.Thread(target=_run, daemon=True, name="claim-redeemable")
    t.start()
