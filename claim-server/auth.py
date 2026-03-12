"""Auth — claim-server wrapper.

Re-exports derive_safe_address from core.auth_shared so that
claim-server/main.py can continue importing ``from auth import derive_safe_address``.
"""

from core.auth_shared import derive_safe_address  # noqa: F401
