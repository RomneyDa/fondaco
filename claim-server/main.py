"""Claim server — periodically redeems resolved Polymarket positions.

Runs as a long-lived process. Checks for redeemable positions every
CLAIM_INTERVAL seconds (default 300 = 5 minutes) and redeems them
via the Builder Relayer.

Required env vars:
    POLYMARKET_PRIVATE_KEY
    POLYMARKET_BUILDER_API_KEY
    POLYMARKET_BUILDER_SECRET
    POLYMARKET_BUILDER_PASSPHRASE

Optional env vars:
    CLAIM_INTERVAL  — seconds between claim cycles (default: 300)
"""

import logging
import os
import sys
import time

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

    interval = int(os.environ.get("CLAIM_INTERVAL", "300"))

    # Derive Safe address once at startup
    from auth import derive_safe_address
    safe_address = derive_safe_address(private_key)
    logger.info("Safe address: %s", safe_address)
    logger.info("Claim interval: %ds", interval)

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

        time.sleep(interval)


if __name__ == "__main__":
    main()
