# Polymarket Up/Down Trading System

Trade crypto up/down prediction markets on Polymarket. Supports multiple market durations simultaneously (e.g. `btc-updown-5m-` and `btc-updown-15m-`).

## The Market

New market every 5 min. **Up** and **Down** tokens — winner pays $1, loser $0 (Chainlink oracle). Book is thin: best_bid=0.01, best_ask=0.99 on both tokens. The **midpoint** (market maker activity) is the real signal (~0.50 at open → ~0.99 on winning side). Edge comes from **picking direction**, not better fills. Both executors use FAK at best_ask/best_bid. Early: midpoint noisy; late (3-4 min): trends strongly; 4.5+ min: losing side's book vanishes.

## Architecture

Simulator and bot share all core logic via `core/`.

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
# Backtest with compound sizing (20% of bankroll per session, uncapped)
uv run python -m simulator.backtest btc-updown-5m --strategy VolBreakout --recent 700 \
  --cash 100 --session-cash-ratio 0.20:99999

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
  defaults.py            Global constants (API URLs, price precision, fees, paths)
  markets.py             Market-specific constants (window duration, gap thresholds)
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

**Taker fees (crypto markets):** `fee = tokens × price × 0.25 × (price × (1-price))^2`. Max 1.56% at p=0.50. Constants in `core/defaults.py`, computed by `compute_taker_fee()`. Both executors deduct fees via `position.cash -= fee`. Backtest: fees on by default, `--no-fees` to disable.

**Guardrails:** Min order 5 tokens. Per-session risk via `--session-cash-ratio` or `--session-cap`. Settlement cooldown in `Trader.process_tick()`. Balance cached to `.cache/balance.json`.

## Regime Detection & Gap Handling

### Data Gap Handling

Session data can have gaps (hours of missing sessions due to downtime, API issues, etc.). The system detects and handles these:

- **`_gap_before_seconds`**: Attached to each session's metadata during backtesting. The time since the previous session. Large gaps (>600s for 5-min markets) indicate stale data.
- **`_data_density`**: Fraction of expected sessions actually present in the lookback window. Below 80% means the lookback data has significant holes.
- **`SessionStatsCache.prior_contiguous()`**: Returns prior sessions stopping at the first large gap. Strategies using this get only temporally dense data instead of lookback windows that span hours of missing data.
- **`SessionStatsCache.data_density()`**: Computes how many sessions exist vs how many should exist based on the time span.

Gap threshold is market-specific: each market's `max_session_gap_seconds` (defined in `core/markets.py`) defaults to 2x its window duration (600s for 5-min, 1800s for 15-min). Data density has no global cutoff; strategies opt in via `min_density` on `RegimeGatedStrategy` (e.g., `min_density = 0.80` to require 80% of expected sessions present).

### Regime Classification

`core/volatility.py` classifies the market regime from prior session data:

- **Volatility regime** (`classify_vol_regime`): `low` (<3 bps mean abs return), `medium` (3-8 bps), `high` (>8 bps). Determines whether the market is moving enough to trade.
- **Trend regime** (`classify_trend_regime`): Uses lag-1 autocorrelation of signed returns. `trending` (>0.15 autocorr = outcomes persist), `mean_reverting` (<-0.15 = outcomes reverse), `flat` (near zero).
- **Data quality**: `clean` or `stale` based on gap and density thresholds.
- **`classify_regime(metadata)`**: Combined classifier returning all three dimensions.

### Regime-Gated Strategies

`RegimeGatedStrategy` (in `core/strategies/regime_gated.py`) wraps any strategy with a regime filter. Subclasses set which regimes they accept:

```python
class TrendDrift(RegimeGatedStrategy):
    required_vol = {"medium", "high"}    # only trade when vol is sufficient
    required_trend = {"trending"}         # only in trending regimes
    skip_stale_data = True                # sit out after data gaps
```

Regime is classified once per session in `on_session_start()`. If the regime doesn't match, `on_tick()` returns HOLD for the entire session. **Regime thresholds are NOT tunable** — they are fixed per strategy class to prevent overfitting the gate itself.

**Active regime-gated strategies:**

| Strategy | Vol Gate | Trend Gate | Entry Logic |
|---|---|---|---|
| `TrendDrift` | medium, high | trending | BTC drift + velocity confirmation |
| `MeanRevFader` | medium, high | mean_reverting | Buy dips below threshold, sell on TP/SL |
| `VolBreakout` | high | any | BTC displacement breakout |
| `ConfirmedLate` | medium, high | any | Late-session high-confidence with confirmation timer |

These strategies are designed to **activate selectively** — each only trades in the ~30-50% of sessions where its edge exists, and sits out the rest. This is the key to avoiding the "great on 50 sessions, flat on 500" problem: instead of one strategy trying to work everywhere, multiple strategies each work in their niche.

## Backtesting & Optimization

**Two-phase backtest: Sharpe for edge, Kelly for sizing.**

- **Phase 1 (Strategy Selection):** Grid search with flat cash per session (default) or compound mode (`--session-cash-ratio R:M` deploys `R × bankroll` per session, capped at `$M`, bankroll carries forward). Ranks by Sharpe to find the best risk-adjusted edge.
- **Phase 2 (Kelly Sizing):** Takes the best params' raw return series and computes `f* = argmax_f Σ log(1 + f·r_i)`. Applies fractional Kelly (default half-Kelly, `--kelly-fraction 0.5`) to get `cash_pct` — the fraction of bankroll to deploy per session. Projects compound growth at that sizing.

**Metrics:**
- **Entry%** (`entry_rate`): fraction of sessions where the strategy traded (at least one order). Regime-gated strategies may only enter 2-5% of sessions.
- **Win%** (`win_rate`): fraction of *traded* sessions with positive PnL. Sitting out is not counted as a loss — Win% measures accuracy when the strategy chooses to act.

The optimizer uses the same two-phase approach and translates `cash_pct` into the live `session-cash-ratio` setting. Constants: `KELLY_FRACTION = 0.5` (engine.py), `MIN_SHARPE_IMPROVEMENT = 0.05` (optimizer.py).

**Why half-Kelly:** Full empirical Kelly is upward-biased from estimation error and regime shifts. Half-Kelly gives ~75% of growth with much less variance. Use `--kelly-fraction 0.25` for conservative, `1.0` for aggressive.

**Laplace smoothing (Rule of Three) for total-loss markets:** In binary-outcome markets (token → $0 or $1), a loss is -100% of deployed capital. With small wins (+4-6%) and total losses, Kelly is extremely sensitive to the estimated loss rate. When the sample has zero observed losses, naive Kelly says f*=1.0 (bet everything) — which is dangerously overfit. We inject synthetic total-loss events using the **Rule of Three**: with 0 losses in n trials, the 95% CI upper bound on loss rate ≈ 3/n. We inject `max(1, round(3*n_traded/(n_traded+3)))` synthetic losses (approaches 3 for large n). This scales with sample size — unlike a fixed 1-loss injection which underestimates risk as n grows.

**Caveat:** Kelly assumes returns scale linearly with size. If fills/slippage change materially with size (liquidity limits, market impact), the true optimal sizing is lower.

### Optimizer Anti-Overfit Measures

The optimizer (`app/optimizer.py`) includes safeguards against deploying overfit parameters:

- **Walk-forward validation**: Before accepting new params, the optimizer splits sessions into overlapping train/validate windows and checks that the best params produce positive Sharpe on each validation fold. If any fold has negative Sharpe, the update is rejected and current params are kept. This prevents deploying params that only worked on one lucky window.

- **Sharpe decay detection**: Compares the strategy's Sharpe on the most recent 1/3 of sessions vs the full lookback window. If recent Sharpe has dropped significantly below the full-window Sharpe (and recent Sharpe is below `min_sharpe`), the strategy is disabled immediately — don't wait for the full-window average to catch up. This catches regime changes faster.

- **Gap-aware session data**: The optimizer now uses `pre_slice_summaries` with the `SessionStatsCache` to attach gap and density metadata to sessions. Regime-gated strategies automatically skip sessions after data gaps, preventing stale lookback data from corrupting their regime estimates.

### Performance: Keeping Backtests Fast

Backtesting time scales as **variants × sessions × ticks_per_session**. The biggest lever is reducing the variant count — a 15,000-variant grid takes 100x longer than a 150-variant one, and the extra coverage rarely changes the winner.

**Two-stage screening (`--screen N`):** For large grids, `--screen 50` runs all variants on 1/3 of sessions first (the most recent third), keeps only the top 50 by Sharpe, then validates those on the full session set. This gives 3-5x speedup on large grids while almost always finding the same winner.

```bash
# 15K variants: ~40 min without screening → ~10 min with --screen 50
uv run python -m simulator.backtest btc-updown-5m --strategy all --screen 50 --recent 30

# Moderate grids (<500 variants) usually don't need --screen
uv run python -m simulator.backtest btc-updown-5m --strategy LateSniper --recent 30
```

**Iterative grid refinement** (most important pattern): Don't run the full Cartesian product. Start coarse, narrow to the winning region, then polish. Each sweep should be < 1000 combos.

```bash
# Step 1: Coarse — full param_grid(), find winning region
uv run python -m simulator.backtest btc-updown-5m --strategy DriftSniper --recent 30 --screen 50

# Step 2: Refine — narrow to 2-3 values per param around the winner
uv run python -m simulator.backtest btc-updown-5m --strategy DriftSniper --recent 30 \
  --param return_threshold_bps=8.0,10.0,12.0 --param min_elapsed=0.35,0.45

# Step 3: Polish — fine steps, stop when top results cluster (similar Sharpe)
uv run python -m simulator.backtest btc-updown-5m --strategy DriftSniper --recent 30 \
  --param return_threshold_bps=9.0,10.0,11.0 --param min_elapsed=0.38,0.40,0.42

# Step 4: Validate — run the winner on 500+ sessions to check for overfit
uv run python -m simulator.backtest btc-updown-5m --strategy DriftSniper --recent 500
```

**Signs you're done refining:** The top 3-5 param combos have Sharpe within 0.05 of each other. Further refinement is fitting noise, not signal.

**Beware overfitting on small samples:** 30 sessions is enough to find promising directions but will overfit. A strategy that looks great on 30 sessions may be flat or negative on 500. Always validate the final winner on `--recent 500` (a few days of data) before trusting Sharpe numbers or deploying live.

**Param grid design guidelines:**
- **If params seem independent, sweep one at a time** — no grid needed when the other values are sensible. Vary one param while holding the rest at reasonable defaults, then move to the next. A 6-param strategy with 4 values each is 4,096 combos as a grid but only 24 runs one-at-a-time. Use a grid only when you suspect params interact (e.g., `min_elapsed` and `confidence_threshold` often do).
- Keep each param to 3-4 values (3^6 = 729, 4^6 = 4,096 — one extra value per param compounds fast)
- Put `None` variants (optional features like stop_loss) in a separate refinement pass: test with/without first, then tune the value
- Binary/categorical params (side_mode, etc.) multiply the grid — include only if you have reason to believe both values are competitive

**Other speed knobs:**
- `--recent 15` for quick exploration, `--recent 30` for coarse grid sweeps, `--recent 500` for final validation. 30 sessions will overfit — always validate winners on 500+ sessions (~a few days of data) before trusting Sharpe numbers
- Run one strategy at a time (`--strategy X`) instead of `--strategy all` — avoids grid explosion across multiple strategies
- The optimizer already does edge-refinement automatically (extends grid when best params hit a boundary)

## Claim Server

The claim server (`claim-server/`) runs as a separate long-lived process that periodically redeems resolved positions via the Builder Relayer.

**Why separate:** Polymarket's Builder Relayer enforces a rate limit of ~100 redemptions per day. Running claims inline after every session would exhaust this quickly. The claim server batches redemptions on a 15-minute interval (configurable via `CLAIM_INTERVAL` env var) to stay well within limits.

```bash
cd claim-server
cp ../.env .env  # needs the same POLYMARKET_* credentials
uv sync
uv run python main.py
```

The main trading bot also has auto-claim enabled (fires after sessions with trades), but the claim server is the primary mechanism.
