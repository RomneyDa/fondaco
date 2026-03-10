# Polymarket Up/Down Trading System

Collect data, backtest strategies, and trade live on Polymarket crypto up/down prediction markets. Originally built for the **5-minute BTC up/down market** (`btc-updown-5m-`).

## The 5-Minute BTC Up/Down Market

A new market opens every 5 minutes. Two tokens — **Up** and **Down** — are minted. At resolution, if BTC price (Chainlink oracle) is higher than at open, Up pays $1 / Down pays $0, and vice versa.

**Book structure:** The CLOB book is extremely thin — typically just **best_bid=0.01** and **best_ask=0.99** on both tokens (0.98 spread, no intermediate orders). The **midpoint** (from market maker activity, not the visible book) is the real signal — it evolves from ~0.50 at open to ~0.99 on the winning side by resolution.

**Execution implications:**
- Edge comes from **picking the right direction**, not better fills
- Both `SimExecutor` and `ClobExecutor` default to **book execution** (FAK at best_ask for buys, best_bid for sells) — backtests match live
- 0.01/0.99 spread is normal; no spread guardrail
- Early (0-2 min): midpoint noisy (0.40-0.55); late (3-4 min): trends strongly; 4.5+ min: losing side's book vanishes (ask=None)

## Architecture

### Simulate First, Trade Live

Simulator and bot share all core logic via `core/`. Strategies, position math, trade sizing, and the tick loop (`Trader.process_tick`) are identical. Only the Executor differs: `SimExecutor` replays history, `ClobExecutor` places real orders.

**Hard constraint:** Any trading behavior change MUST go through `core/` so it can be validated in simulation before risking real money. Never add trading logic only in `app/`.

### Multi-Market Architecture: AppManager + MarketRunners

One central process manages multiple markets. Each market has its own data stream and bot workers:

```
AppManager (one process, port 8420)
├── HTTP ControlServer (daemon thread)
├── MarketRunner "btc-updown-5m" (thread)
│   ├── TickBroadcaster (data/markets/btc-updown-5m/ticks.sock)
│   ├── PriceFetcher, SessionRecorder
│   └── Bot Workers (subprocesses)
│       ├── LateSniper (PID 1234)
│       └── DigitalOption (PID 1235)
├── MarketRunner "btc-updown-15m" (thread)
│   └── ...
└── Shared: LoggingSession, RTDSClient (BTC/USD feed)
```

**Key properties:**
- **Data stream never dies** — killing/restarting a strategy doesn't touch the collector
- **Per-strategy isolation** — each bot is a separate process; crash one, others keep running
- **Hot reload** — `reload` kills and respawns a worker, picking up code changes
- **Multi-market** — one process manages all markets, shared resources (HTTP session, RTDS WebSocket)
- **Market-on/market-off** — start/stop individual markets without affecting others
- **A market ALWAYS has a data stream** — `market-off` kills bots + data; `trade-off` only kills bots

The AppManager broadcasts ticks as JSON lines over per-market Unix sockets. Workers (`app/bot_worker.py`) connect, receive ticks, and run their strategy via `Trader.process_tick()`. Each worker has its own Position, Executor, and state file.

State file at `data/app.json` tracks PID, port, and active markets. Per-market strategy configs persisted to `data/markets/{slug_key}/strategies.json` for `--restore`.

### Strategy Organization

Strategy base class (`Strategy` ABC) lives in `core/strategies/base.py` as shared infrastructure. Concrete implementations live in `bots/`, organized by market type:

- `bots/updown/` — strategies for crypto up/down markets (13 active, 6 archived)
- `bots/__init__.py` — registries: `MARKET_STRATEGIES`, `ALL_STRATEGIES`, `get_strategies_for_market()`
- Import strategies from `bots`, not `core.strategies`: `from bots import ALL_STRATEGIES`
- Import base class from core: `from core.strategies.base import Strategy`

## Usage

```bash
uv sync

# App lifecycle
uv run python -m app start                              # Start central app (empty, no markets)
uv run python -m app start --restore                    # Start + restore all markets & strategies
uv run python -m app stop                               # Immediate stop (all markets + bots)
uv run python -m app kill                               # Graceful: stop all after current session
uv run python -m app status                             # Global status

# Market lifecycle
uv run python -m app market-on btc-updown-5m-           # Start data collection
uv run python -m app market-on btc-updown-5m- --restore # Start + restore strategies for this market
uv run python -m app market-off btc-updown-5m-          # Stop market (kills bots + data)
uv run python -m app market-off btc-updown-5m- --after-session  # Graceful stop

# Bot lifecycle (slug_prefix is first positional arg)
uv run python -m app trade-on btc-updown-5m- --strategy LateSniper --cash 20 --session-cash-ratio 0.25:3
uv run python -m app trade-on btc-updown-5m- --strategy LateSniper --cash 100 --session-cap 5
uv run python -m app trade-off btc-updown-5m- --strategy LateSniper
uv run python -m app trade-off btc-updown-5m-           # Kill all bots on this market
uv run python -m app reload btc-updown-5m- --strategy LateSniper
uv run python -m app set-cash btc-updown-5m- 50 --strategy LateSniper
uv run python -m app set-cash btc-updown-5m- 50 --session-cash-ratio 0.25:3  # Update ratio

# Standalone claim (no running app needed)
uv run python -m app claim [--no-wait]

# Backtest (slug key shorthand resolves to data/markets/{slug}/sessions)
uv run python -m simulator.backtest btc-updown-5m --strategy all --top 10 --recent 30

# Override param grid from CLI (no file edits needed)
uv run python -m simulator.backtest btc-updown-5m --strategy LateSniper --recent 30 \
  --param min_elapsed=0.45,0.50,0.55 --param stop_loss_pct=0.03,0.05
```

Known prefixes in `slugs.json` (git-ignored). Use `--window` matching the slug (300 for 5m, 900 for 15m).

### Per-Session Cash Limits

| Flag | Effect |
|---|---|
| `--per-session` | Reset cash to `--cash` at each session start. No accumulation. |
| `--session-cap <N>` | Deploy at most $N per session; excess held in reserve. Bankroll grows/shrinks but per-session risk is capped. |
| `--session-cash-ratio R:M` | Deploy `min(R * bankroll, M)` per session. Scales with bankroll — winning strategies deploy more, losing ones deploy less. |

**`--session-cash-ratio` is the typical choice.** Example: `--cash 20 --session-cash-ratio 0.25:3` → $20 bankroll, deploys 25% ($5) per session up to $3 max. Win $4 → bankroll=$24, next session deploys min(25%×$24, $3) = $3. Lose $2 → bankroll=$18, next session deploys min(25%×$18, $3) = $3. Lose more → bankroll=$8, deploys min(25%×$8, $3) = $2.

`--session-cap` is the simpler alternative — fixed dollar amount per session regardless of bankroll.

**Updating on a running worker:**
```bash
uv run python -m app set-cash btc-updown-5m- 20 --session-cash-ratio 0.25:3
uv run python -m app set-cash btc-updown-5m- 20 --session-cap 5
uv run python -m app set-cash btc-updown-5m- 20 --clear-session-limit
```

## File Structure

```
core/
  defaults.py               Timing, API URLs, market-aware path helpers (BASE_DIR = project root)
  models.py                 PriceSample, MarketMetadata, SessionData
  api_logger.py             LoggingSession — all HTTP requests go through this
  trader.py                 TradeAction, Position, TickContext, Trade, Executor ABC,
                            SimExecutor, ClobExecutor, Trader (shared tick loop)
  tick_broadcast.py         TickBroadcaster (Unix socket server), TickSubscriber (client)
  rtds_client.py            WebSocket client for Chainlink prices (resolution source)
  market_discovery.py       Gamma API market lookup and resolution
  price_fetcher.py          BTC price + CLOB token price polling
  window_ops.py             Window wait, metadata retry, resolution polling
  session_recorder.py       Session JSON I/O and logging (accepts per-market paths)
  strategies/
    base.py                 Strategy ABC (on_session_start, on_tick, param_grid)
bots/
  __init__.py               MARKET_STRATEGIES, ALL_STRATEGIES, get_strategies_for_market()
  updown/
    __init__.py             STRATEGIES dict — all updown strategy implementations
    late_sniper.py, bargain_hunter.py, ...
    archived/               Strategies that don't work with 0.01/0.99 book
app/
  __main__.py               CLI: start, market-on/off, trade-on/off, reload, status, stop, kill, claim
  app_manager.py            AppManager (central manager for multiple MarketRunners)
  market_runner.py          MarketRunner (data collection + worker management per market)
  bot_worker.py             Bot worker process (one per strategy, connects to tick socket)
  server.py                 HTTP control server (stdlib, daemon thread)
  auth.py                   CLOB auth helpers (Safe derivation, API creds, cache)
  claim.py                  Claim/redeem logic via Builder Relayer
simulator/
  engine.py                 Simulator, SessionResult, BacktestResult, load_sessions
  backtest.py               CLI grid search runner (per-market backtest logs)
dashboard/                  React dashboard (separate)
data/
  app.json                  App state (pid, port, active markets)
  app.log                   Global app log
  api_raw/                  API request logs (global)
  markets/
    btc-updown-5m/
      sessions/             Collected session JSON files
      live/
        session.json        Current live session (updated every 2s)
      workers/
        LateSniper/
          state.json        Worker state (cash, position)
          live_trades.json   Current session trades
          worker.log         Worker stdout/stderr
      ticks.sock            Unix socket for tick broadcast
      bot_trades.jsonl      Per-market trade log
      bot_log.jsonl         Per-market session log
      strategies.json       Per-market strategy restore config
      backtest_log.jsonl    Per-market backtest results
```

## HTTP Control API

All endpoints on port 8420. Bot lifecycle endpoints require `slug_prefix` as a top-level field in the JSON body.

```
GET  /api/control/status                         # {running, markets: [...]}
POST /api/control/stop                           # Stop everything
POST /api/control/kill                           # Stop after current sessions
POST /api/control/market-on                      # {slug_prefix, window?, market_type?, restore?}
POST /api/control/market-off                     # {slug_prefix, after_session?}
POST /api/control/trade-on                       # {slug_prefix, strategy, params?, cash?, ...}
POST /api/control/trade-off                      # {slug_prefix, strategy?}
POST /api/control/reload                         # {slug_prefix, strategy}
POST /api/control/set-cash                       # {slug_prefix, cash, strategy?, session_cash_ratio?, session_cap?, clear_session_limit?}
```

## Tick Scale

~18 ticks/sec. A 5-min session ≈ 5,400 ticks; 15-min ≈ 16,200. Strategy params (EMA spans, lookbacks) are in **ticks**, not seconds. `slow_span=200` ≈ 11 seconds. BTC variance per tick is tiny (~0.01–0.15 bps), so slope/momentum thresholds must be correspondingly small.

## CLOB API Constraints

| Constraint | Value |
|---|---|
| Min order size | 5 tokens (`CLOB_MIN_ORDER_TOKENS`) |
| Tick size | 0.01 |
| Price range | 0.01–0.99 (2 decimal places) |
| Order type | FAK (Fill-and-Kill) |
| neg_risk | False |
| Book spread | Typically 0.01/0.99 |

Buy sizes floored to integer. Sell sizes `math.floor` to 4 decimals — never round up sells.

## API Latency

CLOB ~300ms, Gamma ~270ms, Data API ~375ms, Builder Relayer ~675ms. Network RTT ~45ms (Cloudflare, US East). Negligible for 5-minute windows.

## Guardrails

| Guardrail | Constant | Default |
|---|---|---|
| Min order size | CLOB_MIN_ORDER_TOKENS | 5 tokens |

Per-session risk controlled by `--session-cash-ratio` (scales with bankroll) or `--session-cap` (fixed amount). Settlement cooldown prevents selling before on-chain settlement; enforced in `Trader.process_tick()`. Balance cached to `.cache/balance.json`; use `--cash cached` to start from real balance.

## Strategy Param Optimization

Use iterative grid refinement — coarse grid → zoom in → zoom in. Keep each sweep **< 1000 combos**.

1. **Coarse sweep** — Use the strategy's built-in `param_grid()`:
   ```bash
   uv run python -m simulator.backtest btc-updown-5m --strategy MyStrategy --top 10 --recent 30
   ```
2. **Refine** — Override specific params via `--param` (no file edits needed):
   ```bash
   uv run python -m simulator.backtest btc-updown-5m --strategy MyStrategy --recent 30 \
     --param min_elapsed=0.45,0.48,0.50,0.52,0.55 --param stop_loss_pct=0.02,0.03,0.04
   ```
   Unspecified params still use the strategy's `param_grid()` values. Use a single value to pin: `--param max_price=0.95`.
3. **Polish (optional)** — Even finer `--param` steps. Stop when top results cluster tightly.

## CRITICAL RULES — REAL MONEY

This bot trades real money. No sandbox, no undo.

1. **All trades flow through `ClobExecutor.execute_trade`** — no ad-hoc scripts, no direct `create_and_post_order` calls outside the execution path.
2. **Read before modifying trade execution code** — before changing `ClobExecutor.execute_trade`, `SimExecutor.execute_trade`, `Position.compute_trade_size`, `Position.apply_trade`, `Position.resolve`, or `Trader.process_tick`: read it, understand it, confirm no path where money is lost silently.
3. **Position must stay in sync** — `position.apply_trade()` called only after successful CLOB order. Failed order = position unchanged.
4. **Private keys are sacred** — loaded from `.env` via `python-dotenv`, never logged/printed/committed.
5. **Log everything** — orders → `data/markets/{slug}/bot_trades.jsonl`, sessions → `data/markets/{slug}/bot_log.jsonl`, API calls → `data/api_raw/`, backtests → `data/markets/{slug}/backtest_log.jsonl`.
6. **When in doubt, do not trade** — ask the user.

## Rules

- **Always pass `--recent 30`** (or less) when running backtests.
- **uv** for package management. `uv sync`, `uv add <pkg>`, `uv run python -m <module>`.
- **Python >=3.10** — use `typing.Optional`/`typing.Union`, not `X | Y`.
- **All HTTP requests through `LoggingSession`** — no bare `requests.get()`.
- **Auth**: `.env` has `POLYMARKET_PRIVATE_KEY`. Derives Gnosis Safe via Polygon RPC (`CTF_EXCHANGE` at `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`), caches in `.cache/auth.json` (git-ignored, auto-invalidates on key change). Lazy-initialized when trading is enabled.
