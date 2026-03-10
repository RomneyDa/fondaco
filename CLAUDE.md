# Polymarket Up/Down Trading System

Trade crypto up/down prediction markets on Polymarket. Built for the **5-minute BTC up/down market** (`btc-updown-5m-`).

## The Market

New market every 5 min. **Up** and **Down** tokens — winner pays $1, loser $0 (Chainlink oracle). Book is thin: best_bid=0.01, best_ask=0.99 on both tokens. The **midpoint** (market maker activity) is the real signal (~0.50 at open → ~0.99 on winning side). Edge comes from **picking direction**, not better fills. Both executors use FAK at best_ask/best_bid. Early: midpoint noisy; late (3-4 min): trends strongly; 4.5+ min: losing side's book vanishes.

## Architecture

Simulator and bot share all core logic via `core/`. **Hard constraint:** trading behavior changes MUST go through `core/` — never add trading logic only in `app/`.

```
AppManager (port 8420)
├── ControlServer (daemon thread)
├── MarketRunner per market (thread) — TickBroadcaster, PriceFetcher, SessionRecorder, Bot Workers (subprocesses)
├── Optimizer (subprocess, optional) — periodic backtest + param update
└── Shared: LoggingSession, RTDSClient
```

**Key properties:** Data stream never dies (kill/restart bots freely) · Per-strategy isolation (separate processes) · Hot reload with mid-session join · Multi-market · `market-off` kills bots+data, `trade-off` only kills bots

Ticks broadcast as JSON lines over per-market Unix sockets. Workers connect, receive ticks, run `Trader.process_tick()`. State at `data/app.json`; per-market configs at `data/markets/{slug_key}/strategies.json`.

### Mid-Session Join

When a worker connects mid-session (optimizer reload, `trade-on`), TickBroadcaster replays `session_start` with `replayed: true`. Strategies with `requires_full_session = False` (default) trade immediately. Those with `True` skip to next session.

| `requires_full_session = True` | `= False` (default) |
|---|---|
| CheapShot, DigitalOption, PoissonKelly, PoissonKellyActive, PoissonKellyHistorical, DynamicPoissonKelly, PoissonKellyStacked | LateSniper, BargainHunter, BrendaFlipper, BuyTrend, DipSniper, FadeTheMarket, MidpointMomentum, MomentumEntry |

Set `True` when strategy accumulates state from tick 0 (jump counts, warmup windows, running sums).

### Strategies

Base class: `core/strategies/base.py` (`Strategy` ABC — `on_session_start`, `on_tick`, `param_grid`, `requires_full_session`). Implementations: `bots/updown/`. Registries: `bots/__init__.py` (`ALL_STRATEGIES`, `get_strategies_for_market()`). Import from `bots`, not `core.strategies`.

**Versioning:** `version` class attribute (simple integers). Bump on logic changes, once per editing session.

## Usage

```bash
uv sync
# App: start [--restore] | stop | kill | status
uv run python -m app start --restore

# Markets: market-on | market-off [--after-session]
uv run python -m app market-on btc-updown-5m- --restore

# Bots: trade-on | trade-off | reload | set-cash
uv run python -m app trade-on btc-updown-5m- --strategy LateSniper --cash 20 --session-cash-ratio 0.25:3
uv run python -m app trade-off btc-updown-5m- --strategy LateSniper
uv run python -m app reload btc-updown-5m- --strategy LateSniper
uv run python -m app set-cash btc-updown-5m- 50 --session-cash-ratio 0.25:3

# Optimizer: optimizer-on | optimizer-off | optimizer-config
uv run python -m app optimizer-on --interval 15 --lookback 20 --min-sharpe 0.15

# Backtest
uv run python -m simulator.backtest btc-updown-5m --strategy all --top 10 --recent 30
uv run python -m simulator.backtest btc-updown-5m --strategy LateSniper --recent 30 \
  --param min_elapsed=0.45,0.50,0.55 --param stop_loss_pct=0.03,0.05

# Claim
uv run python -m app claim [--no-wait]
```

### Per-Session Cash Limits

`--session-cash-ratio R:M` (typical): deploy `min(R × bankroll, M)` per session. Scales with wins/losses.
`--session-cap N`: fixed per-session amount. `--per-session`: reset to `--cash` each session.

Example: `--cash 20 --session-cash-ratio 0.25:3` → deploys 25% up to $3.

Update live: `set-cash btc-updown-5m- 20 --session-cash-ratio 0.25:3` / `--session-cap 5` / `--clear-session-limit`

## File Structure

```
core/
  defaults.py            Timing, API URLs, market-aware path helpers
  models.py              PriceSample, MarketMetadata, SessionData
  api_logger.py          LoggingSession
  trader.py              TradeAction, Position, TickContext, Trade, Executor ABC, SimExecutor, ClobExecutor, Trader
  tick_broadcast.py      TickBroadcaster (replays session_start to mid-session joiners), TickSubscriber
  rtds_client.py         Chainlink WebSocket client
  market_discovery.py    Gamma API market lookup/resolution
  price_fetcher.py       BTC + CLOB token price polling
  window_ops.py          Window wait, metadata retry, resolution polling
  session_recorder.py    Session JSON I/O
  strategies/base.py     Strategy ABC
bots/
  __init__.py            Registries (ALL_STRATEGIES, MARKET_STRATEGIES)
  updown/                Strategy implementations; archived/ for deprecated ones
app/
  __main__.py            CLI entry point
  app_manager.py         AppManager (multi-market + optimizer)
  market_runner.py       MarketRunner (data + workers per market)
  bot_worker.py          Worker process (one per strategy)
  optimizer.py           Live optimizer subprocess
  server.py              HTTP control server
  auth.py                CLOB auth (Safe derivation, creds, cache)
  claim.py               Claim/redeem via Builder Relayer
simulator/
  engine.py              Simulator, SessionResult, BacktestResult, load_sessions
  backtest.py            CLI grid search runner
dashboard/               React dashboard
data/
  app.json               App state (pid, port, markets)
  markets/{slug}/        sessions/, live/, workers/, ticks.sock, bot_trades.jsonl,
                         bot_log.jsonl, strategies.json, backtest_log.jsonl
  optimizer/             status.json, config.json, events.jsonl, optimizer.log
```

## HTTP Control API (port 8420)

```
GET  /api/control/status                    POST /api/control/stop|kill
POST /api/control/market-on|market-off      POST /api/control/trade-on|trade-off|reload|set-cash
GET  /api/control/optimizer-status|events    POST /api/control/optimizer-on|off|config
```

Bot endpoints require `slug_prefix` in JSON body.

## Technical Reference

**Tick scale:** ~18 ticks/sec. 5-min ≈ 5,400 ticks. Params in ticks, not seconds (`slow_span=200` ≈ 11s). BTC variance per tick ~0.01-0.15 bps.

**CLOB constraints:** Min 5 tokens, tick 0.01, range 0.01-0.99, FAK orders, neg_risk=False, spread typically 0.01/0.99. Buy sizes floored to int; sell sizes `math.floor` to 4 decimals.

**API latency:** CLOB ~300ms, Gamma ~270ms, Data ~375ms, Builder ~675ms. Negligible for 5-min windows.

**Taker fees (crypto markets):** `fee = tokens × price × 0.25 × (price × (1-price))^2`. Max 1.56% at p=0.50. Constants in `core/defaults.py`, computed by `compute_taker_fee()`. Both executors deduct fees via `position.cash -= fee`. Backtest: `--fee-bps` (default 1000).

**Guardrails:** Min order 5 tokens. Per-session risk via `--session-cash-ratio` or `--session-cap`. Settlement cooldown in `Trader.process_tick()`. Balance cached to `.cache/balance.json`.

## Backtesting & Optimization

**Two-phase backtest: Sharpe for edge, Kelly for sizing.**

- **Phase 1 (Strategy Selection):** Grid search with flat cash per session (`compound=False`). Ranks by Sharpe to find the best risk-adjusted edge. This gives clean, unscaled per-session returns.
- **Phase 2 (Kelly Sizing):** Takes the best params' raw return series and computes `f* = argmax_f Σ log(1 + f·r_i)`. Applies fractional Kelly (default half-Kelly, `--kelly-fraction 0.5`) to get `cash_pct` — the fraction of bankroll to deploy per session. Projects compound growth at that sizing.

The optimizer uses the same two-phase approach and translates `cash_pct` into the live `session-cash-ratio` setting. Constants: `KELLY_FRACTION = 0.5` (engine.py), `MIN_SHARPE_IMPROVEMENT = 0.05` (optimizer.py).

**Why half-Kelly:** Full empirical Kelly is upward-biased from estimation error and regime shifts. Half-Kelly gives ~75% of growth with much less variance. Use `--kelly-fraction 0.25` for conservative, `1.0` for aggressive.

**Laplace smoothing for total-loss markets:** In binary-outcome markets (token → $0 or $1), a loss is -100% of deployed capital. With small wins (+4-6%) and total losses, Kelly is extremely sensitive to the estimated loss rate. When the sample has zero observed losses, naive Kelly says f*=1.0 (bet everything) — which is dangerously overfit. We inject 1 synthetic total-loss event into the return series before computing Kelly. This gives realistic sizing: e.g. with ~20 wins and 1 synthetic loss, Kelly f*≈0.25, half-Kelly→12.5% per session.

**Caveat:** Kelly assumes returns scale linearly with size. If fills/slippage change materially with size (liquidity limits, market impact), the true optimal sizing is lower.

Iterative grid refinement, keep each sweep < 1000 combos. Always `--recent 30` or less.

1. **Coarse:** `--strategy MyStrategy --top 10 --recent 30` (uses `param_grid()`)
2. **Refine:** `--param min_elapsed=0.45,0.48,0.50 --param stop_loss_pct=0.02,0.03` (unspecified use grid defaults)
3. **Polish:** finer steps, stop when top results cluster

## CRITICAL RULES — REAL MONEY

1. **All trades through `ClobExecutor.execute_trade`** — no ad-hoc order scripts
2. **Read before modifying** execution code (`execute_trade`, `compute_trade_size`, `apply_trade`, `resolve`, `process_tick`)
3. **Position in sync** — `apply_trade()` only after successful CLOB order
4. **Private keys sacred** — `.env` via `python-dotenv`, never logged/committed
5. **Log everything** — trades, sessions, API calls, backtests all to `data/`
6. **When in doubt, do not trade**

## Rules

- **Bump strategy version** on logic changes (once per editing session)
- **`--recent 30`** or less for all backtests
- **uv** for packages (`uv sync`, `uv add`, `uv run python -m`)
- **Python >=3.10** — use `typing.Optional`/`typing.Union`, not `X | Y`
- **All HTTP through `LoggingSession`** — no bare `requests.get()`
- **Auth:** `.env` has `POLYMARKET_PRIVATE_KEY`, derives Safe via Polygon RPC, caches in `.cache/auth.json`
