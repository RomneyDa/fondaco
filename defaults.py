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
POLL_INTERVAL_SECONDS = 2.5
WINDOW_DURATION_SECONDS = 300
RESOLUTION_POLL_INTERVAL = 5.0
RESOLUTION_TIMEOUT_SECONDS = 300

# Collection
TARGET_SESSIONS = 100

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "sessions")
LIVE_DIR = os.path.join(BASE_DIR, "data", "live")
LOG_FILE = os.path.join(BASE_DIR, "data", "collection_log.jsonl")
COLLECTOR_LOG_FILE = os.path.join(BASE_DIR, "data", "collector.log")
API_RAW_DIR = os.path.join(BASE_DIR, "data", "api_raw")
