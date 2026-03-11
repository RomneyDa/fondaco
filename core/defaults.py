"""Default values for the Polymarket data collector.

All of these can be overridden via CLI flags except API endpoints and paths.
The slug prefix (e.g. "btc-updown-5m-") is required at the CLI — there is
no default.
"""

import os

# API endpoints
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
COINBASE_BTC_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
COINGECKO_BTC_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"

# Timing
POLL_INTERVAL_SECONDS = 0.055
WINDOW_DURATION_SECONDS = 300
RESOLUTION_POLL_INTERVAL = 5.0
RESOLUTION_TIMEOUT_SECONDS = 300

# Collection
TARGET_SESSIONS = 100

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_RAW_DIR = os.path.join(BASE_DIR, "data", "api_raw")

# Trading guardrails (shared by simulator and bot)
CLOB_MIN_ORDER_TOKENS = 5          # Min order size in tokens (CLOB rejects below this)
TRADE_CUTOFF_SECONDS = 5           # Stop all trades this many seconds before session end
DEFAULT_RATIO_MAX = 100            # Default max $ per session in --session-cash-ratio
OPTIMIZER_MAX_RATIO = 0.12         # Optimizer may never set session-cash-ratio above 12%

# CLOB taker fee (crypto up/down markets)
# Ref: https://docs.polymarket.com/trading/fees
# Crypto: feeRate=0.25, exponent=2.  Max effective fee = 1.56% at price 0.50.
# fee = tokens * price * feeRate * (price * (1 - price))^exponent
CRYPTO_FEE_RATE = 0.25
CRYPTO_FEE_EXPONENT = 2
def compute_taker_fee(price, tokens, enabled=True):
    # type: (float, float, bool) -> float
    """Compute CLOB taker fee in dollars.

    Polymarket crypto fee: fee = C * p * feeRate * (p * (1-p))^exponent
    where feeRate=0.25, exponent=2 for crypto markets.
    Max ~1.56% of notional at price 0.50.
    """
    if not enabled or price <= 0 or price >= 1.0 or tokens <= 0:
        return 0.0
    fee = tokens * price * CRYPTO_FEE_RATE * (price * (1.0 - price)) ** CRYPTO_FEE_EXPONENT
    return fee

# Balance cache
BALANCE_CACHE_FILE = os.path.join(BASE_DIR, ".cache", "balance.json")

# App (central process)
APP_STATE_FILE = os.path.join(BASE_DIR, "data", "app.json")
APP_LOG_FILE = os.path.join(BASE_DIR, "data", "app.log")
DEFAULT_CONTROL_PORT = 8420

# Multi-market data layout: data/markets/{slug_key}/...
MARKETS_DIR = os.path.join(BASE_DIR, "data", "markets")


def _slug_key(slug_prefix):
    # type: (str) -> str
    """Normalize slug prefix to directory key (strip trailing dash)."""
    return slug_prefix.rstrip("-")


def market_data_dir(slug_key):
    # type: (str) -> str
    return os.path.join(MARKETS_DIR, slug_key)


def market_sessions_dir(slug_key):
    # type: (str) -> str
    return os.path.join(MARKETS_DIR, slug_key, "sessions")


def market_live_dir(slug_key):
    # type: (str) -> str
    return os.path.join(MARKETS_DIR, slug_key, "live")


def market_socket_path(slug_key):
    # type: (str) -> str
    # Unix sockets have a 104-char path limit on macOS.
    # Use /tmp to keep the path short.
    return "/tmp/eros-pm-{}.sock".format(slug_key)


def market_workers_dir(slug_key):
    # type: (str) -> str
    return os.path.join(MARKETS_DIR, slug_key, "workers")


def market_strategies_file(slug_key):
    # type: (str) -> str
    return os.path.join(MARKETS_DIR, slug_key, "strategies.json")


def market_trades_file(slug_key):
    # type: (str) -> str
    return os.path.join(MARKETS_DIR, slug_key, "bot_trades.jsonl")


def market_session_log(slug_key):
    # type: (str) -> str
    return os.path.join(MARKETS_DIR, slug_key, "bot_log.jsonl")


def market_backtest_log(slug_key):
    # type: (str) -> str
    return os.path.join(MARKETS_DIR, slug_key, "backtest_log.jsonl")


# Optimizer
OPTIMIZER_DIR = os.path.join(BASE_DIR, "data", "optimizer")


def optimizer_status_file():
    # type: () -> str
    return os.path.join(OPTIMIZER_DIR, "status.json")


def optimizer_events_file():
    # type: () -> str
    return os.path.join(OPTIMIZER_DIR, "events.jsonl")


def optimizer_config_file():
    # type: () -> str
    return os.path.join(OPTIMIZER_DIR, "config.json")
