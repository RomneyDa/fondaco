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
DATA_DIR = os.path.join(BASE_DIR, "data", "sessions")
LIVE_DIR = os.path.join(BASE_DIR, "data", "live")
LOG_FILE = os.path.join(BASE_DIR, "data", "collection_log.jsonl")
COLLECTOR_LOG_FILE = os.path.join(BASE_DIR, "data", "collector.log")
API_RAW_DIR = os.path.join(BASE_DIR, "data", "api_raw")
BACKTEST_LOG_FILE = os.path.join(BASE_DIR, "data", "backtest_log.jsonl")

# Trading guardrails (shared by simulator and bot)
CLOB_MIN_ORDER_TOKENS = 5          # Min order size in tokens (CLOB rejects below this)
MAX_POSITION_FRACTION = 0.50       # Max fraction of total balance in positions
MAX_POSITION_DOLLARS = 10_000.0    # Hard cap on position value
MAX_LOSS_FRACTION = 0.30           # Max loss as fraction of reference balance
MAX_LOSS_DOLLARS = 1_000.0         # Hard cap on loss per session/day

# Balance cache
BALANCE_CACHE_FILE = os.path.join(BASE_DIR, ".cache", "balance.json")

# App (unified process)
APP_STATE_FILE = os.path.join(BASE_DIR, "data", "app.json")
APP_LOG_FILE = os.path.join(BASE_DIR, "data", "app.log")
DEFAULT_CONTROL_PORT = 8420
