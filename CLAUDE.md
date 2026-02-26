# Polymarket Up/Down Trading System

Collect data, backtest strategies, and trade live on Polymarket crypto up/down prediction markets. This system was originally built for the **5-minute BTC up/down market** (`btc-updown-5m-`).

## The 5-Minute BTC Up/Down Market

A new market opens every 5 minutes. Two tokens are minted — **Up** and **Down**. At resolution (5 minutes later), if BTC price (Chainlink oracle) is higher than at market open, the Up token pays $1 and Down pays $0, and vice versa.

**Book structure:** The CLOB order book on these markets is extremely thin. Typically only two price levels exist: **best_bid=0.01** and **best_ask=0.99** on both tokens, giving a 0.98 spread. There are no intermediate resting orders. The **midpoint** (derived from market maker activity, not the visible book) is the real signal — it smoothly evolves from ~0.50 at open to ~0.99 on the winning side by resolution.

**Implications for execution:**
- The edge comes from **picking the right direction**, not from getting a better fill price
- Both `SimExecutor` and `ClobExecutor` default to **book execution** (FAK at best_ask for buys, best_bid for sells) — backtests match live behavior
- The 0.01/0.99 spread is normal and expected; there is no spread guardrail
- Early in the session (0-2 min), midpoint is noisy (0.40-0.55); by 3-4 min it trends strongly; by 4.5+ min the losing side's book vanishes entirely (ask=None)

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
uv run python -m app trade-on --strategy DigitalOption --cash 10 --session-cap 5  # $10 bankroll, $5/session
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

### Per-Session Cash Limits

Two flags control how cash is allocated across sessions:

| Flag | Effect |
|---|---|
| `--per-session` | Reset `position.cash` to the `--cash` amount at the start of every session. Wins/losses don't carry over. |
| `--session-cap <N>` | Deploy at most $N per session; hold excess in reserve. Reserve is restored after resolution, so the bankroll grows/shrinks but per-session risk is capped. |

**`--session-cap` is the typical choice for bankroll management.** Example: `--cash 10 --session-cap 5` starts with a $10 bankroll and risks $5 per session. If the first session wins $2, the bankroll becomes $12 and the next session still deploys $5 (with $7 in reserve). Use `--per-session` only when you want a hard fixed budget with no accumulation.

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
    archived/               Strategies that don't work with 0.01/0.99 book (round-trip sellers)
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
| Book spread | Typically 0.01/0.99 (default `book` execution = FAK at best_ask/best_bid) |

Buy token sizes are floored to integer in `ClobExecutor` for clean maker amounts. Sell token sizes use `math.floor` to 4 decimals — never round up sells.

## API Latency

CLOB order round-trip (TTFB) is ~300ms. Gamma ~270ms, Data API ~375ms, Builder Relayer ~675ms. Network RTT to servers (Cloudflare, US East) is ~45ms; rest is TLS + server processing. Negligible for 5-minute windows.

## Guardrails (shared, constants in core/defaults.py)

| Guardrail | Constant | Default |
|---|---|---|
| Min order size | CLOB_MIN_ORDER_TOKENS | 5 tokens |
| Max position exposure | MAX_POSITION_FRACTION / MAX_POSITION_DOLLARS | 50% of balance or $10,000 |
| Max loss per session/day | MAX_LOSS_FRACTION / MAX_LOSS_DOLLARS | 30% of balance or $1,000 |

Enforced in `Trader.process_tick()` (shared tick loop). App caches balance to `.cache/balance.json`; use `--cash cached` in backtest/app to start from real balance.

## Strategy Param Optimization Workflow

When tuning a strategy's parameters, use iterative grid refinement — not a single massive grid.

1. **Coarse sweep** — Design an initial `param_grid()` with wide spacing that produces **< 1000 total combinations**. Multiply out the grid dimensions to verify before running. Example: 5 values × 5 values × 4 values = 100 combos. Run the backtest:
   ```bash
   uv run python -m simulator.backtest data/sessions/btc-updown-5m --strategy MyStrategy --top 10 --recent 30
   ```

2. **Refine around the optimum** — Read the best params from the leaderboard output. Build a tighter grid centered on those values with finer step sizes, again keeping total combos under ~1000. Override `param_grid()` (or use `--params` for a single run) and re-run.

3. **Final polish (optional)** — Repeat once more with even finer steps around the new best. Diminishing returns after 2–3 rounds — stop when the top results are clustered tightly.

This is a numerical-methods-style approach: coarse grid → zoom in → zoom in. It avoids combinatorial explosion (a 6-param grid with 10 values each = 1M combos) while still finding near-optimal params efficiently.

## CRITICAL RULES — REAL MONEY

This bot trades real money. No sandbox, no undo.

1. **All trades flow through `ClobExecutor.execute_trade`** — no ad-hoc scripts, no direct `create_and_post_order` calls outside the app's execution path.

2. **Read before modifying trade execution code** — before changing `ClobExecutor.execute_trade`, `SimExecutor.execute_trade`, `Position.compute_trade_size`, `Position.apply_trade`, `Position.resolve`, or `Trader.process_tick`: read it, understand it, confirm no path where money is lost silently.

3. **Position must stay in sync** — `position.apply_trade()` is called only after a successful CLOB order. Failed order = position unchanged. No exceptions.

4. **Private keys are sacred** — loaded from `.env` via `python-dotenv`, never logged/printed/committed. `.env` is gitignored.

5. **Log everything** — every order attempt → `data/bot_trades.jsonl`, every session → `data/bot_log.jsonl`, every API call → `data/api_raw/`. Never bypass logging. Backtest runs → `data/backtest_log.jsonl`.

6. **When in doubt, do not trade** — ask the user. The cost of asking is zero.

## Rules

- **Always pass `--recent 30`** (or less) when running backtests. Never run against the full session history — it's slow and the oldest data is least relevant.
- **uv** for package management. `uv sync` to install, `uv add <pkg>` to add deps. Run via `uv run python -m <module>`.
- **Python >=3.10** — use `typing.Optional`/`typing.Union`, not `X | Y` syntax.
- **All HTTP requests through `LoggingSession`** — no bare `requests.get()`. Logs to `data/api_raw/{host}/{date}.jsonl`.
- **Auth**: `.env` has `POLYMARKET_PRIVATE_KEY`. App derives Gnosis Safe address via Polygon RPC (`CTF_EXCHANGE` at `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`), caches creds in `.cache/auth.json` (git-ignored, auto-invalidates on key change). Auth is lazy-initialized when trading is enabled.
