"""Safe address derivation for Polymarket.

Self-contained module (no dependency on core/).  Derives the deterministic
CREATE2 Gnosis Safe address via CTFExchange.getSafeAddress(address).
"""

import logging

logger = logging.getLogger(__name__)

# CTF Exchange contract on Polygon.
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"


def derive_safe_address(private_key):
    # type: (str) -> str
    """Derive the Polymarket Gnosis Safe address from a private key.

    Calls CTFExchange.getSafeAddress(address) via Polygon RPC.
    Tries multiple RPC endpoints with fallback.
    """
    import requests
    from eth_account import Account
    from eth_abi import encode, decode
    from eth_utils import keccak

    eoa_address = Account.from_key(private_key).address
    logger.info("EOA address: %s", eoa_address)

    fn_selector = keccak(b"getSafeAddress(address)")[:4]
    calldata = "0x" + fn_selector.hex() + encode(["address"], [eoa_address]).hex()

    rpc_urls = [
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon.llamarpc.com",
    ]
    for rpc_url in rpc_urls:
        try:
            resp = requests.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{"to": CTF_EXCHANGE, "data": calldata}, "latest"],
                    "id": 1,
                },
                timeout=10,
            )
            result = resp.json()
            if "result" in result and len(result["result"]) >= 42:
                decoded = decode(["address"], bytes.fromhex(result["result"][2:]))
                return decoded[0]
        except (requests.RequestException, ValueError, KeyError):
            continue

    raise RuntimeError("Failed to derive Safe address from any Polygon RPC")
