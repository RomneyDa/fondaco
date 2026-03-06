# Multi-Market Architecture Reorganization (v2)

## Context

The system currently runs one supervisor process per market (slug prefix), each with its own port and PID. As we expand beyond BTC 5-minute markets, this doesn't scale — we need one central app managing multiple markets as sub-entities. The dashboard should reflect this hierarchy: app > markets > bots.

## Key Design Decisions

### 1. Market Types vs Individual Slugs

A "market" in our system is a **market type** — a slug prefix pattern (e.g., `btc-updown-5m-`) representing a stream of recurring sessions. This is what a MarketRunner manages.

For future sports/event betting, the model differs fundamentally:
- Crypto up/down: repeating time-windowed sessions with predictable slug pattern (`{prefix}{timestamp}`)
- Sports: one-shot events with unique slugs (e.g., `lakers-celtics-2026-03-05`), variable duration, different discovery

The MarketRunner base should be abstract enough to support both modes:
- **TimeWindowedRunner** (current): loops on fixed-duration windows, auto-discovers next slug
- **EventRunner** (future): discovers events by criteria, manages each as a one-shot session

For now we only implement TimeWindowedRunner. The abstraction boundary is MarketRunner.run() — anything above it (AppManager, ControlServer, worker spawning) is mode-agnostic.

### 2. Strategies Per Market Type

Currently `ALL_STRATEGIES` is a single global registry. All strategies are crypto up/down specific (they assume up/down tokens, BTC price, time-windowed sessions). Sports strategies would have completely different signals.

Solution: **market-type strategy registries**.

```python
# core/strategies/__init__.py
UPDOWN_STRATEGIES = { ... }  # current strategies

# Future:
# SPORTS_STRATEGIES = { ... }

# Backward compat + combined registry
ALL_STRATEGIES = {**UPDOWN_STRATEGIES}

# Per market-type lookup
MARKET_STRATEGIES = {
    "updown": UPDOWN_STRATEGIES,
}
```

MarketRunner validates strategies against its market type. The CLI/dashboard filter the strategy list per market.

### 3. Backtest Per Market

Backtest log and sessions are per-market. Each market type has its own sessions dir, backtest log, and bot logs. The backtest CLI defaults to a market's sessions dir.

### 4. slug_prefix in HTTP API

All multi-market endpoints pass `slug_prefix` as a **top-level string field** in the JSON body. No URL parameter parsing, no nested objects. The handler does `body.get("slug_prefix")` and validates it's non-empty.

```python
# Example handler
body = self._read_json_body()
slug_prefix = body.get("slug_prefix")
if not slug_prefix:
    return self._send_json({"error": "Missing 'slug_prefix'"}, 400)
runner = self.app_manager.get_market(slug_prefix)
```

## New Architecture

```
AppManager (one process, port 8420)
+-- HTTP ControlServer (daemon thread)
+-- MarketRunner "btc-updown-5m" (thread)
|   +-- TickBroadcaster (data/markets/btc-updown-5m/ticks.sock)
|   +-- PriceFetcher, SessionRecorder
|   +-- Bot Workers (subprocesses)
|       +-- LateSniper (PID 1234)
|       +-- DigitalOption (PID 1235)
+-- MarketRunner "btc-updown-15m" (thread)
|   +-- ...
+-- Shared: LoggingSession, RTDSClient (BTC/USD feed)
```

**Key rules:**
- A market ALWAYS has a data stream. `market-off` kills bots + data.
- Killing bots (`trade-off`) doesn't kill the data stream.
- Bots can be hot-restarted (`reload`), have stateful JSON.
- `--restore` on `start` restores all markets + strategies. Per-market `--restore` on `market-on`.

## New Data Layout

```
data/
+-- app.json                    # {pid, port, markets: ["btc-updown-5m", ...]}
+-- app.log                     # Global app log
+-- api_raw/                    # API request logs (global)
+-- markets/
    +-- btc-updown-5m/
        +-- sessions/           # Session JSON files
        +-- live/
        |   +-- session.json    # Current live session (updated every 2s)
        +-- workers/
        |   +-- LateSniper/
        |       +-- state.json
        |       +-- live_trades.json
        |       +-- worker.log
        +-- ticks.sock          # Unix socket for tick broadcast
        +-- bot_trades.jsonl    # Per-market trade log
        +-- bot_log.jsonl       # Per-market session log
        +-- backtest_log.jsonl  # Per-market backtest results
        +-- strategies.json     # Per-market strategy restore config
```

**Migration**: Move existing files manually as part of implementation (no migration code in app).

## New CLI

```bash
# App lifecycle
app start                              # Start central app (empty, no markets)
app start --restore                    # Start + restore all markets & strategies
app stop                               # Immediate stop (all markets + bots)
app status                             # Global status

# Market lifecycle
app market-on btc-updown-5m-           # Start data collection
app market-on btc-updown-5m- --restore # Start + restore strategies for this market
app market-off btc-updown-5m-          # Stop market (kills bots + data)
app market-off btc-updown-5m- --after-session  # Graceful stop

# Bot lifecycle (slug is positional)
app trade-on btc-updown-5m- --strategy LateSniper --cash 100 --session-cap 5
app trade-off btc-updown-5m- --strategy LateSniper
app trade-off btc-updown-5m-           # Kill all bots on this market
app reload btc-updown-5m- --strategy LateSniper
app set-cash btc-updown-5m- 50 --strategy LateSniper

# Standalone
app claim [--no-wait]
```

## New HTTP API

All POST bodies include `slug_prefix` as a direct top-level string field.

```
# App-level
GET  /api/control/status                         # {running, markets: [...]}
POST /api/control/stop                           # Stop everything

# Market lifecycle
POST /api/control/market-on                      # {slug_prefix, window?}
POST /api/control/market-off                     # {slug_prefix, after_session?}

# Bot lifecycle (slug_prefix in body)
POST /api/control/trade-on                       # {slug_prefix, strategy, params, cash, ...}
POST /api/control/trade-off                      # {slug_prefix, strategy?}
POST /api/control/reload                         # {slug_prefix, strategy}
POST /api/control/set-cash                       # {slug_prefix, cash, strategy?}
```

## Implementation Steps

### Phase 1: Strategy registry per market type

**`core/strategies/__init__.py`** — Reorganize:
- Rename `ALL_STRATEGIES` -> `UPDOWN_STRATEGIES` (the actual dict)
- `ALL_STRATEGIES = {**UPDOWN_STRATEGIES}` for backward compat
- Add `MARKET_STRATEGIES = {"updown": UPDOWN_STRATEGIES}`
- Add `get_strategies_for_market(market_type: str) -> Dict` helper
- Default market_type is "updown"

### Phase 2: Core defaults & paths

**`core/defaults.py`** — Add market-aware path helpers:
- `MARKETS_DIR = data/markets`
- `market_data_dir(slug_key)` -> `data/markets/{slug_key}`
- `market_sessions_dir(slug_key)` -> `data/markets/{slug_key}/sessions`
- `market_live_dir(slug_key)` -> `data/markets/{slug_key}/live`
- `market_socket_path(slug_key)` -> `data/markets/{slug_key}/ticks.sock`
- `market_workers_dir(slug_key)` -> `data/markets/{slug_key}/workers`
- `market_strategies_file(slug_key)` -> `data/markets/{slug_key}/strategies.json`
- `market_trades_file(slug_key)` -> `data/markets/{slug_key}/bot_trades.jsonl`
- `market_session_log(slug_key)` -> `data/markets/{slug_key}/bot_log.jsonl`
- `market_backtest_log(slug_key)` -> `data/markets/{slug_key}/backtest_log.jsonl`

Remove old single-market path constants (`TICK_SOCKET_DIR`, `WORKERS_DIR`, `STRATEGIES_CONFIG_FILE`, `DATA_DIR`, `LIVE_DIR`, `LOG_FILE`, `BACKTEST_LOG_FILE`) after updating all references.

### Phase 3: Update session_recorder and other core modules

**`core/session_recorder.py`** — Accept `sessions_dir` and `live_dir` params instead of using global constants:
- `__init__(self, slug_prefix, sessions_dir=None, live_dir=None, log_file=None)`
- Falls back to market-aware path helpers if not provided
- This lets both the MarketRunner and simulator pass custom paths

**`core/window_ops.py`** — No changes needed (no path dependencies).

**`core/price_fetcher.py`** — No changes needed.

### Phase 4: MarketRunner (refactor Runner)

**`app/market_runner.py`** — Copy from `app/runner.py`, modify:
- Rename `Runner` -> `MarketRunner`
- Accept `slug_prefix`, `market_type` (default "updown"), shared `http_session`, `rtds_client`
- Remove signal handling (AppManager handles that)
- Use market-aware path helpers everywhere
- `run()` becomes a thread target (loops until `self.running = False`)
- Worker spawn passes `--market-dir` to bot_worker
- `_save_strategies_config()` writes to per-market `strategies.json`
- Validate strategies against `MARKET_STRATEGIES[market_type]`
- `get_status()` returns market-level status (no global info)

### Phase 5: AppManager (new central manager)

**`app/app_manager.py`** — New file:
- Owns shared resources: `LoggingSession`, `RTDSClient`, `ThreadPoolExecutor`
- `_markets: Dict[str, MarketRunner]` (thread-safe with lock)
- `start_market(slug_prefix, window_duration, market_type="updown")` -> create MarketRunner, start on thread
- `stop_market(slug_prefix, after_session=False)` -> stop runner + its workers
- `get_market(slug_prefix)` -> return MarketRunner (raises if not found)
- `get_status()` -> aggregate status from all markets
- `run()` -> block on main thread, handle signals, propagate stop
- `_save_state()` -> write `app.json` with pid, port, list of active markets + their configs
- `restore()` -> read `app.json` for market list, start each, each restores strategies

### Phase 6: Bot worker updates

**`app/bot_worker.py`** — Add `--market-dir` CLI arg:
- State dir: `{market_dir}/workers/{strategy}/`
- Trade log: `{market_dir}/bot_trades.jsonl`
- Session log: `{market_dir}/bot_log.jsonl`
- Socket path derived from `{market_dir}/ticks.sock` (or still passed via `--socket`)
- All file paths derived from `--market-dir`, no more global constants for data paths
- Add `--market-type` arg (default "updown") for strategy validation

### Phase 7: HTTP server updates

**`app/server.py`** — Replace `runner` with `app_manager`:
- All endpoints that need a market: `slug_prefix = body.get("slug_prefix")`, validate non-empty
- `status` returns `app_manager.get_status()` with per-market breakdown
- `market-on` / `market-off` call `app_manager.start_market()` / `stop_market()`
- `trade-on`, `trade-off`, `reload`, `set-cash`: extract `slug_prefix` from body, look up market runner, delegate
- No URL parameter parsing — slug always in JSON body for POST, or query param for GET

### Phase 8: CLI updates

**`app/__main__.py`**:
- `start` — No slug arg. Creates AppManager, ControlServer. Optional `--restore`.
- `market-on <slug>` — HTTP POST to start market. Optional `--restore`, `--window`, `--market-type`.
- `market-off <slug>` — HTTP POST to stop market. Optional `--after-session`.
- `trade-on <slug>`, `trade-off <slug>`, `reload <slug>`, `set-cash <slug>` — slug becomes first positional arg. Passed as `slug_prefix` in body.
- `status` — Shows all markets and their bots.
- `stop` — Stops everything.
- `claim` — Unchanged.

### Phase 9: Simulator compatibility

**`simulator/engine.py`** — `load_sessions()` already accepts a path. No changes needed.

**`simulator/backtest.py`**:
- Default sessions path: if positional arg looks like a slug key (no `/`), resolve to `data/markets/{slug_key}/sessions`
- Backtest log: write to `data/markets/{slug_key}/backtest_log.jsonl` when sessions are under a market dir
- Fall back to old `data/backtest_log.jsonl` path when using arbitrary paths
- Accept `--market-type` to filter available strategies (default: all)

### Phase 10: Manual data migration

```bash
mkdir -p data/markets/btc-updown-5m/{sessions,live,workers}
mv data/sessions/btc-updown-5m/* data/markets/btc-updown-5m/sessions/
mv data/live/btc-updown-5m.json data/markets/btc-updown-5m/live/session.json  # if exists
mv data/workers/* data/markets/btc-updown-5m/workers/  # all current workers
mv data/sockets/btc-updown-5m.sock data/markets/btc-updown-5m/ticks.sock  # if exists
mv data/strategies.json data/markets/btc-updown-5m/strategies.json
mv data/bot_trades.jsonl data/markets/btc-updown-5m/bot_trades.jsonl
mv data/bot_log.jsonl data/markets/btc-updown-5m/bot_log.jsonl
mv data/backtest_log.jsonl data/markets/btc-updown-5m/backtest_log.jsonl
# Keep: data/app.json, data/app.log, data/api_raw/
```

Update `.gitignore` to cover `data/markets/`.

### Phase 11: Dashboard backend

**`dashboard/server/server.ts`**:
- `MARKETS_DIR = data/markets`
- `GET /api/prefixes` -> glob `data/markets/*/sessions/` for market slugs
- `GET /api/sessions/:prefix/...` -> read from `data/markets/{prefix}/sessions/`
- `GET /api/live` -> scan `data/markets/*/live/`
- `GET /api/live/:prefix` -> read from `data/markets/{prefix}/live/session.json`
- `GET /api/bot/status` -> returns multi-market status from control server
- `GET /api/bot/trades` -> scan `data/markets/*/bot_trades.jsonl` + worker live trades
- Bot control proxies pass `slug_prefix` in request body
- Add `POST /api/bot/market-on`, `POST /api/bot/market-off` proxies
- File watcher paths: `data/markets/**`
- SSE events include market slug

### Phase 12: Dashboard frontend

**Types** (`dashboard/src/types.ts`):
- Add `MarketStatus` interface (slug, running, session_count, strategies, tick_clients)
- Add `AppStatus` interface: `{running, markets: MarketStatus[]}`
- Keep `BotStatus` as alias for backward compat during transition

**API** (`dashboard/src/api.ts`):
- Add `startMarket(slug_prefix, opts)`, `stopMarket(slug_prefix, opts)`
- Bot control functions gain `slug_prefix` parameter: `tradeBotOn({slug_prefix, strategy, ...})`
- `fetchBotStatus()` returns `AppStatus`
- `startBot()` -> removed (replaced by `startMarket`)

**Components**:
- `Sidebar.tsx` — Markets section replaces single supervisor. Each market expandable with its bots.
- `BotPanel.tsx` -> Per-market controls. Market selector for trade-on. Market-on form replaces start-supervisor form.
- `LiveView.tsx` — Already prefix-aware, path updates for new data layout.

## Files to Create/Modify

| File | Action |
|------|--------|
| `core/defaults.py` | Modify — add market path helpers, remove old paths |
| `core/strategies/__init__.py` | Modify — add UPDOWN_STRATEGIES, MARKET_STRATEGIES |
| `core/session_recorder.py` | Modify — accept path params |
| `app/app_manager.py` | Create — central AppManager class |
| `app/market_runner.py` | Create — refactored from runner.py |
| `app/runner.py` | Delete (after market_runner.py is done) |
| `app/bot_worker.py` | Modify — add `--market-dir` arg |
| `app/server.py` | Modify — multi-market endpoints |
| `app/__main__.py` | Modify — new CLI structure |
| `simulator/backtest.py` | Modify — per-market backtest log, slug shorthand |
| `.gitignore` | Modify — add data/markets/ |
| `dashboard/server/server.ts` | Modify — multi-market API |
| `dashboard/src/types.ts` | Modify — add market types |
| `dashboard/src/api.ts` | Modify — market-scoped calls |
| `dashboard/src/components/Sidebar.tsx` | Modify — market hierarchy |
| `dashboard/src/components/BotPanel.tsx` | Modify — per-market controls |

## Tests

### Unit Tests

**`tests/test_defaults.py`** — New:
- `test_market_data_dir()` — verify path construction for various slug keys
- `test_market_sessions_dir()` — verify nested path
- `test_market_socket_path()` — verify socket path ends with `.sock`
- `test_all_market_paths_under_markets_dir()` — every helper returns path under `data/markets/`
- `test_slug_key_stripping()` — verify `btc-updown-5m-` -> `btc-updown-5m` in paths

**`tests/test_strategy_registry.py`** — New:
- `test_updown_strategies_exist()` — UPDOWN_STRATEGIES is non-empty, all values are Strategy subclasses
- `test_all_strategies_is_superset()` — ALL_STRATEGIES contains all UPDOWN_STRATEGIES
- `test_market_strategies_lookup()` — `MARKET_STRATEGIES["updown"]` == UPDOWN_STRATEGIES
- `test_get_strategies_for_unknown_market()` — returns empty or raises
- `test_no_duplicate_strategy_names()` — no name collisions across registries

**`tests/test_session_recorder.py`** — New:
- `test_custom_paths()` — SessionRecorder with custom sessions_dir/live_dir writes to correct locations
- `test_default_paths_use_market_helpers()` — SessionRecorder without custom paths uses market-aware defaults
- `test_save_and_load_session()` — round-trip session save/exists/load with custom dir
- `test_live_session_write_and_clear()` — live file created/removed in correct dir

**`tests/test_app_manager.py`** — New:
- `test_start_market()` — AppManager.start_market creates a MarketRunner entry
- `test_start_duplicate_market()` — starting same market twice raises/returns error
- `test_stop_market()` — stopping a market removes it and kills workers
- `test_get_status_empty()` — status with no markets
- `test_get_status_with_markets()` — status includes market entries
- `test_save_and_restore_state()` — app.json written with market list, restored on restart
- `test_stop_all()` — stopping app stops all markets

**`tests/test_market_runner.py`** — New:
- `test_enable_trading_validates_strategy()` — unknown strategy raises ValueError
- `test_enable_trading_validates_market_type()` — strategy not in market type raises
- `test_worker_spawn_passes_market_dir()` — spawned subprocess cmd includes `--market-dir`
- `test_strategies_config_path()` — saves to per-market strategies.json
- `test_get_status_structure()` — status dict has expected keys
- `test_disable_trading()` — worker removed from list
- `test_restore_strategies()` — reads per-market strategies.json and spawns workers

**`tests/test_bot_worker.py`** — New/extend:
- `test_market_dir_arg_sets_paths()` — `--market-dir` sets state_dir, trade log, session log
- `test_state_save_under_market_dir()` — state.json written to `{market_dir}/workers/{strategy}/`
- `test_trade_log_under_market_dir()` — trades appended to `{market_dir}/bot_trades.jsonl`
- `test_session_log_under_market_dir()` — session log appended to `{market_dir}/bot_log.jsonl`

**`tests/test_control_server.py`** — New:
- `test_status_endpoint()` — GET /api/control/status returns app-level status
- `test_market_on_missing_slug()` — POST /api/control/market-on without slug_prefix returns 400
- `test_market_on_success()` — POST with valid slug_prefix returns ok
- `test_trade_on_missing_slug()` — POST /api/control/trade-on without slug_prefix returns 400
- `test_trade_on_unknown_market()` — POST with slug_prefix for non-running market returns 404
- `test_trade_on_success()` — POST with valid slug + strategy returns ok
- `test_trade_off_all()` — POST /api/control/trade-off with slug kills all bots on that market
- `test_stop_endpoint()` — POST /api/control/stop stops everything
- `test_set_cash_with_slug()` — slug_prefix extracted correctly from body

**`tests/test_backtest_paths.py`** — New:
- `test_slug_shorthand_resolves()` — passing `btc-updown-5m` resolves to `data/markets/btc-updown-5m/sessions`
- `test_backtest_log_per_market()` — backtest writes to `data/markets/{slug}/backtest_log.jsonl`
- `test_explicit_path_still_works()` — passing full path works as before
- `test_backtest_log_fallback()` — non-market paths use global backtest log

### Integration Tests

**`tests/test_multi_market_integration.py`** — New:
- `test_full_lifecycle()` — start app -> market-on -> trade-on -> status -> trade-off -> market-off -> stop
- `test_two_markets_independent()` — start two markets, verify each has independent data dirs, workers, status
- `test_restore_round_trip()` — start app with markets + strategies, stop, restart with --restore, verify same state
- `test_worker_uses_correct_market_dir()` — spawn worker, verify state/logs go to correct market dir
- `test_market_off_kills_workers()` — market-off stops all bots for that market only

### CLI Tests

**`tests/test_cli.py`** — New:
- `test_start_no_slug()` — `app start` succeeds (no positional slug required)
- `test_market_on_requires_slug()` — `app market-on` without slug errors
- `test_trade_on_requires_slug()` — `app trade-on` without slug errors
- `test_status_no_app()` — `app status` when not running prints "not running"

## Verification (Manual)

1. **Unit tests pass**: `uv run pytest tests/ -v`
2. **Start app**: `app start` -> verify empty, no markets
3. **Add market**: `app market-on btc-updown-5m-` -> verify data collection starts
4. **Check status**: `app status` -> verify market listed with session count
5. **Add bot**: `app trade-on btc-updown-5m- --strategy BargainHunter --cash 10 --dry-run --session-cap 5` -> verify trades
6. **Multi-market**: `app market-on btc-updown-15m- --window 900` -> verify both collecting independently
7. **Restore**: `app stop` -> `app start --restore` -> verify markets and strategies restored
8. **Backtest**: `uv run python -m simulator.backtest btc-updown-5m --strategy all --top 10 --recent 30`
9. **Dashboard**: Markets show up, per-market controls work, live view works, session history works
10. **Data layout**: Files in correct `data/markets/{slug}/` subdirectories

## Future: Sports/Event Betting

When adding sports (e.g., NBA game markets), the extension points are:

1. **New market type**: Add `"sports"` to `MARKET_STRATEGIES` with sport-specific strategies
2. **EventRunner**: New MarketRunner subclass that discovers events by criteria (API query for "all NBA games today"), manages each as a one-shot session rather than looping on time windows
3. **Discovery**: Sports markets have unique slugs per game, not `{prefix}{timestamp}`. EventRunner would need a different discovery mechanism (e.g., tag-based search on Gamma API)
4. **Session lifecycle**: Game duration is variable. EventRunner would poll for game start/end rather than using fixed windows
5. **Multiple slugs per "market"**: An NBA market runner might manage 10 games simultaneously, each with its own slug. Workers would need to handle multiple concurrent sessions or be spawned per-game.

The current refactor ensures points 1-2 are clean extension points without requiring another architectural overhaul. The key abstraction: MarketRunner.run() is the boundary — everything above (AppManager, HTTP, CLI) is already market-type-agnostic.
