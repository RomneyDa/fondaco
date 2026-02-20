# Polymarket Up/Down Market Data Collector

## Project Goal

Collect historical price data from Polymarket's crypto up/down prediction markets to develop and train trading strategies. Each market is a binary bet on whether a crypto asset will go up or down over a time window (e.g. 5 minutes, 15 minutes).

## How It Works

The collector runs continuously, capturing data for each session:

1. **Discovers** the active market using deterministic slug computation (`{slug_prefix}{unix_timestamp}`)
2. **Polls** at a configurable interval during the window, recording:
   - BTC spot price (from Coinbase/CoinGecko)
   - Up/Down token midpoint prices (from Polymarket CLOB API)
   - Up/Down token best bid/ask (from CLOB order book)
3. **Waits for resolution** and records the outcome (Up or Down)
4. **Saves** each session as a JSON file in `data/sessions/{slug_prefix}/`

## Usage

```bash
pip install -r requirements.txt

# Required: slug prefix. Everything else has sensible defaults.
python collector.py btc-updown-5m-

# Override defaults:
python collector.py btc-updown-5m- --sessions 50
python collector.py eth-updown-5m- --window 300 --poll 2.5
python collector.py btc-updown-15m- --window 900 --poll 5.0 --sessions 200

# See all options:
python collector.py --help
```

### CLI flags

| Flag | Default | Description |
|---|---|---|
| `slug_prefix` (positional, **required**) | — | Market slug prefix, e.g. `btc-updown-5m-` |
| `--sessions` | 100 | Number of sessions to collect |
| `--window` | 300 | Window duration in seconds |
| `--poll` | 2.5 | Poll interval in seconds |
| `--resolution-poll` | 5.0 | Resolution poll interval in seconds |
| `--resolution-timeout` | 300 | Resolution timeout in seconds |

Press Ctrl+C for graceful shutdown (current session is saved).

### Known slug prefixes

See `slugs.json` for a list of known active slug prefixes (git-ignored, fetched from Polymarket). Currently active: `btc-updown-5m-`, `btc-updown-15m-`, `eth-updown-5m-`, `eth-updown-15m-`, `sol-updown-5m-`, `sol-updown-15m-`, `xrp-updown-5m-`, `xrp-updown-15m-`.

## Backtesting

Replay collected session data through trading strategies, optimize parameters via grid search, and compare results.

```bash
# Run all strategies against collected data
python backtest.py data/sessions/btc-updown-5m

# Single strategy
python backtest.py data/sessions/btc-updown-5m --strategy MomentumEntry

# Customize execution model
python backtest.py data/sessions/btc-updown-5m --execution midpoint --slippage 0.03 --cash 200

# Show per-session breakdown for best result
python backtest.py data/sessions/btc-updown-5m --strategy all --top 10 --verbose
```

### Backtest CLI flags

| Flag | Default | Description |
|---|---|---|
| `data_dir` (positional, **required**) | — | Path to session files directory |
| `--strategy` | `all` | Strategy name or `all` |
| `--optimize` | `sharpe` | Metric: `sharpe`, `total_pnl`, `win_rate` |
| `--execution` | `midpoint_slippage` | `midpoint_slippage`, `midpoint`, `book` |
| `--slippage` | 0.02 | Half-spread for midpoint_slippage mode |
| `--cash` | 100 | Starting cash per session |
| `--top` | 10 | Show top N param combos |
| `--verbose` | off | Per-session breakdown for best params |

### Execution modes

The order book bid/ask is typically 0.01/0.99 (98-cent spread), so `book` mode is unrealistic. Default `midpoint_slippage` uses midpoint +/- a configurable half-spread (default 0.02) for more realistic execution.

### Built-in strategies

- **AlwaysBuyUp / AlwaysBuyDown** — baselines, buy at first tick
- **MomentumEntry** — buy direction of BTC move if it exceeds a threshold early in window
- **MidpointMomentum** — follow token midpoint trend over last N ticks
- **LateSniper** — wait until late in window when outcome is more certain
- **FadeTheMarket** — mean reversion: bet against early midpoint extremes

## File Structure

- `collector.py` - CLI entry point and main orchestration loop
- `defaults.py` - Default values for timing, API URLs, paths
- `models.py` - Data classes (PriceSample, MarketMetadata, SessionData)
- `market_discovery.py` - Gamma API market lookup and resolution checking
- `price_fetcher.py` - BTC price and CLOB token price polling
- `api_logger.py` - LoggingSession that records every API request/response
- `session_recorder.py` - JSON file I/O and collection logging
- `simulator.py` - Core backtesting engine (data classes, trade execution, session replay)
- `strategies.py` - Trading strategy definitions (base class + 6 built-in strategies)
- `backtest.py` - CLI runner for grid search and strategy comparison
- `slugs.json` - Known slug prefixes (git-ignored)
- `data/sessions/{slug_prefix}/*.json` - Collected session data
- `data/api_raw/{host}/{date}.jsonl` - Raw API request/response logs (git-ignored)
- `data/collection_log.jsonl` - Append-only run log

## APIs Used

- **Gamma Markets API** (`gamma-api.polymarket.com`) - Market discovery, no auth needed
- **CLOB API** (`clob.polymarket.com`) - Token prices/order books, no auth for reads
- **Coinbase** / **CoinGecko** - BTC spot price, no auth needed

## Rate Limits

~100 req/min per host. The collector makes ~5 requests per 2.5s sample across multiple hosts, staying well within limits. `LoggingSession` handles 429 responses with automatic exponential backoff.

## Session Data Format

Each JSON file contains: market metadata (token IDs, condition ID, timestamps), initial/final BTC price, outcome (Up/Down), and an array of price samples with BTC price, token midpoints, and bid/ask spreads.

## Rules

### Python 3.9 compatibility

The runtime is Python 3.9. Do not use syntax or features added in 3.10+ (e.g. `X | Y` union types, `match` statements, `ParamSpec`). Use `typing.Optional`, `typing.Union`, etc. instead.

### All API requests MUST be logged

Every HTTP request made to any external API must go through `LoggingSession` (from `api_logger.py`), which records the full request and response to `data/api_raw/{host}/{YYYY-MM-DD}.jsonl`. This is non-negotiable — no direct `requests.get()` or bare `requests.Session()` calls. When adding new API calls or new modules, always use the shared `LoggingSession` instance. The raw API logs are git-ignored but must always be captured locally for analysis.

### API log data structure

Logs are organized by API host and date. Each JSONL entry contains: timestamp, method, full URL, host, path, query params, status code, elapsed time in ms, the full JSON response body, and any error. This structure is intentionally generic so the same format works across different Polymarket market types (5-min BTC, 1-min BTC, other assets, etc.).

### Slug prefix is always required

The slug prefix (e.g. `btc-updown-5m-`) must be passed as a positional CLI argument. There is no default — this forces the caller to be explicit about which market they're collecting. The `--window` flag should match the slug's actual window duration (300 for 5m, 900 for 15m, etc.).
