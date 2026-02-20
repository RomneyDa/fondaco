# Polymarket Up/Down Trading System

Collect data, backtest strategies, and trade live on Polymarket crypto up/down prediction markets. Each market is a binary bet on whether an asset goes up or down over a time window (5m, 15m).

## Architecture: Simulate First, Trade Live

The simulator and bot share all core logic via `core/`. Strategies, position math, trade sizing, and the tick loop (`Trader.process_tick`) are identical between backtesting and live trading. The only difference is the Executor: `SimExecutor` replays historical data, `ClobExecutor` places real CLOB orders.

**This is a hard constraint.** Any behavior change to how trades are decided, sized, or executed MUST go through shared code in `core/` so it can be validated in simulation before risking real money. Never add trading logic only in `app/` — if the simulator can't test it, it shouldn't go live.

The `app/` module is a unified process that always collects data and optionally trades. Trading is toggled on/off via CLI or HTTP control server (port 8420). The Runner (`app/runner.py`) manages both modes, snapshotting trading state at session boundaries. State file at `data/app.json` tracks PID + port.

## Usage

```bash
uv sync

# Start unified app (always collects data; optionally trades)
uv run python -m app start btc-updown-5m-
uv run python -m app start btc-updown-5m- --strategy LateSniper --params min_elapsed=0.7 --cash 100 --dry-run

# Toggle trading on a running app (HTTP calls to control server)
uv run python -m app trade-on --strategy LateSniper --params min_elapsed=0.7 --cash 100
uv run python -m app trade-off

# Control
uv run python -m app status
uv run python -m app stop

# Standalone claim (no running app needed)
uv run python -m app claim [--no-wait]

# Backtest strategies
uv run python -m simulator.backtest data/sessions/btc-updown-5m --strategy all --top 10
```

Slug prefix is always required (positional arg). Known prefixes in `slugs.json` (git-ignored). Use `--window` matching the slug (300 for 5m, 900 for 15m).

## File Structure

```
core/
  defaults.py               Timing, API URLs, paths (BASE_DIR resolves to project root)
  models.py                 PriceSample, MarketMetadata, SessionData
  api_logger.py             LoggingSession — all HTTP requests go through this
  trader.py                 TradeAction, Position, TickContext, Trade, Executor ABC,
                            SimExecutor, ClobExecutor, Trader (shared tick loop)
  rtds_client.py            WebSocket client for Chainlink prices (resolution source)
  market_discovery.py       Gamma API market lookup and resolution
  price_fetcher.py          BTC price + CLOB token price polling
  window_ops.py             Window wait, metadata retry, resolution polling
  session_recorder.py       Session JSON I/O and logging
  strategies/
    __init__.py             ALL_STRATEGIES registry
    base.py                 Strategy ABC (on_session_start, on_tick, param_grid)
    always_buy_up.py        Baseline: buy Up at first tick
    always_buy_down.py      Baseline: buy Down at first tick
    momentum_entry.py       BTC momentum threshold entry
    midpoint_momentum.py    Token midpoint trend following
    late_sniper.py          Late-window confident entry
    fade_the_market.py      Mean reversion against early extremes
    ema_crossover.py        Dual EMA crossover, multi-trade with position flips
    ols_slope.py            Rolling OLS regression slope, trend entry/exit
    scalper.py              Mean reversion scalper, take-profit/stop-loss
    trailing_stop_momentum.py  Momentum entry with trailing stop management
app/
  __main__.py               CLI: start, trade-on, trade-off, status, stop, claim
  runner.py                 Runner class (unified collect + trade loop)
  server.py                 HTTP control server (stdlib, daemon thread)
  auth.py                   CLOB auth helpers (Safe derivation, API creds, cache)
  claim.py                  Claim/redeem logic via Builder Relayer
simulator/
  engine.py                 Simulator, SessionResult, BacktestResult, load_sessions
  backtest.py               CLI grid search runner (logs to data/backtest_log.jsonl)
dashboard/                  React dashboard (separate)
data/                       All runtime data (sessions, logs, etc.)
```

## Tick Scale

Sessions sample at ~18 ticks/sec. A 5-minute (300s) session has ~5,400 ticks; a 15-minute (900s) session has ~16,200 ticks. Strategy warmup/lookback params (EMA spans, OLS window sizes, rolling lookbacks) are measured in **ticks**, not seconds. A `slow_span=200` EMA burns only ~11 seconds of warmup in a 5-minute window. Keep param grids calibrated to this scale — a span of 50 means ~3 seconds, not 50 seconds. BTC price variance per tick is tiny (~0.01–0.15 bps), so slope/momentum thresholds in bps/tick must be correspondingly small.

## CLOB API Constraints

| Constraint | Value |
|---|---|
| Min order size | 5 tokens (`CLOB_MIN_ORDER_TOKENS`) |
| Tick size | 0.01 |
| Price range | 0.01–0.99 |
| Price precision | 2 decimal places |
| Order type | FAK (Fill-and-Kill) |
| neg_risk | False |
| Book spread | Typically 0.01/0.99 (use `midpoint_slippage` execution, not `book`) |

Buy token sizes are floored to integer in `ClobExecutor` for clean maker amounts. Sell token sizes use `math.floor` to 4 decimals — never round up sells.

## Guardrails (shared, constants in core/defaults.py)

| Guardrail | Constant | Default |
|---|---|---|
| Min order size | CLOB_MIN_ORDER_TOKENS | 5 tokens |
| Max position exposure | MAX_POSITION_FRACTION / MAX_POSITION_DOLLARS | 50% of balance or $10,000 |
| Max loss per session/day | MAX_LOSS_FRACTION / MAX_LOSS_DOLLARS | 30% of balance or $1,000 |

Enforced in `Trader.process_tick()` (shared tick loop). App caches balance to `.cache/balance.json`; use `--cash cached` in backtest/app to start from real balance.

## CRITICAL RULES — REAL MONEY

This bot trades real money. No sandbox, no undo.

1. **All trades flow through `ClobExecutor.execute_trade`** — no ad-hoc scripts, no direct `create_and_post_order` calls outside the app's execution path.

2. **Read before modifying trade execution code** — before changing `ClobExecutor.execute_trade`, `SimExecutor.execute_trade`, `Position.compute_trade_size`, `Position.apply_trade`, `Position.resolve`, or `Trader.process_tick`: read it, understand it, confirm no path where money is lost silently.

3. **Position must stay in sync** — `position.apply_trade()` is called only after a successful CLOB order. Failed order = position unchanged. No exceptions.

4. **Private keys are sacred** — loaded from `.env` via `python-dotenv`, never logged/printed/committed. `.env` is gitignored.

5. **Log everything** — every order attempt → `data/bot_trades.jsonl`, every session → `data/bot_log.jsonl`, every API call → `data/api_raw/`. Never bypass logging. Backtest runs → `data/backtest_log.jsonl`.

6. **When in doubt, do not trade** — ask the user. The cost of asking is zero.

## Rules

- **uv** for package management. `uv sync` to install, `uv add <pkg>` to add deps. Run via `uv run python -m <module>`.
- **Python >=3.10** — use `typing.Optional`/`typing.Union`, not `X | Y` syntax.
- **All HTTP requests through `LoggingSession`** — no bare `requests.get()`. Logs to `data/api_raw/{host}/{date}.jsonl`.
- **Auth**: `.env` has `POLYMARKET_PRIVATE_KEY`. App derives Gnosis Safe address via Polygon RPC (`CTF_EXCHANGE` at `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`), caches creds in `.cache/auth.json` (git-ignored, auto-invalidates on key change). Auth is lazy-initialized when trading is enabled.
