"""Listen address and token checks for the claim UI."""

from __future__ import annotations

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def resolve_ui_bind(host: str | None, token: str | None) -> tuple[str, str]:
    """Return ``(host, token)``.

    Loopback is the default. A public address is refused unless ``token``
    is non-empty. The token is never written into the HTML page.
    """
    chosen = (host or "127.0.0.1").strip() or "127.0.0.1"
    secret = (token or "").strip()
    if chosen not in _LOOPBACK and not secret:
        raise ValueError(
            "refusing to listen on a non-loopback address without CLAIM_UI_TOKEN"
        )
    return chosen, secret


def token_matches(expected: str, got: str) -> bool:
    """Compare two tokens without returning early on the first difference."""
    if not expected:
        return False
    if len(expected) != len(got):
        return False
    diff = 0
    for left, right in zip(expected, got):
        diff |= ord(left) ^ ord(right)
    return diff == 0
