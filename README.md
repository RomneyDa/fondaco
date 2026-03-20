# Polymarket Up/Down Trading System

Trade crypto up/down prediction markets on Polymarket. Supports multiple market durations simultaneously (e.g. `btc-updown-5m-` and `btc-updown-15m-`).

## The Market

New market every 5 min. **Up** and **Down** tokens — winner pays $1, loser $0 (Chainlink oracle). Book is thin: best_bid=0.01, best_ask=0.99 on both tokens. The **midpoint** (market maker activity) is the real signal (~0.50 at open → ~0.99 on winning side). Edge comes from **picking direction**, not better fills. Early: midpoint noisy; late (3-4 min): trends strongly; 4.5+ min: losing side's book vanishes.

## Architecture

All core logic (strategies, position math, simulation, backtesting) is in Rust. Live execution uses a collector→worker architecture with Unix socket IPC. CLOB order signing is in Python (`clob-bridge/`).

See the **`/architecture`** page in the dashboard for an interactive visual reference.

```
src/                        Rust core library
  lib.rs                    Crate root (15 modules)
  defaults.rs               Global constants (API URLs, precision, fees, paths)
  markets.rs                Market-specific constants (window duration, gap thresholds)
  models.rs                 Sample, MarketMetadata, SessionData, SessionSummary
  trader.rs                 TradeAction, Position, TickContext, Trade, SimExecutor, Trader
  strategy.rs               Strategy trait, Param, ParamValue
  strategies/               8 active strategy implementations + registry
  engine.rs                 Simulator, session loading, Kelly sizing, backtest results
  tick_broadcast.rs         TickBroadcaster/TickSubscriber (Unix socket IPC)
  session_cache.rs          SessionStatsCache (bisect-based lookback)
  session_io.rs             Session JSON + .summary.json sidecar I/O
  rtds.rs                   RTDS WebSocket client (Chainlink BTC price)
  price_fetcher.rs          Sample collection (RTDS → Coinbase → CoinGecko fallback)
  market_discovery.rs       Gamma API market metadata queries
  volatility.rs             Regime classification (vol + trend)
  math_utils.rs             norm_cdf, OLS slope
  window_ops.rs             Window boundary timing

src/bin/
  backtest.rs               Backtest CLI (clap + rayon)
  collector.rs              Data collector daemon (1 per market)
  bot_worker.rs             Trading bot (1 per strategy, reads from collector's tick socket)
  orchestrator.rs           Optional process manager (:8420)

dashboard/                  Next.js 15 + tRPC + Express monitoring UI (:3456)
clob-bridge/                Python Flask CLOB order execution API (:8421)
claim-server/               Python claim/redeem service (:8080)
models/                     ML experiments (standalone, not integrated)
data/                       Runtime data (sessions, logs, trades)
```

### Process Architecture

```
Orchestrator :8420 (optional)          Dashboard :3456
  ├── manages clob-bridge :8421          ├── Next.js 15 App Router + tRPC
  ├── spawns collectors + workers        ├── Express SSE file-watching
  └── HTTP control API                   └── proxies to orchestrator or direct file reads

Collector (1 per market)               Bot Worker (N per market)
  ├── RTDS WebSocket + CLOB polling      ├── TickSubscriber (reads tick.sock)
  ├── SessionStatsCache                  ├── strategy.on_tick() → TradeAction
  ├── writes sessions/ + .summary.json   ├── ClobExecutor → clob-bridge HTTP
  └── tick.sock broadcast                └── writes bot_trades.jsonl
```

Collectors and bot-workers are **fully independent** — the orchestrator is optional convenience for lifecycle management. You can run `./target/release/collector --slug-prefix btc-updown-5m-` and `./target/release/bot-worker --slug-prefix btc-updown-5m- --strategy PoissonKelly` directly without any orchestrator.

### Strategies

Trait: `src/strategy.rs` (`Strategy` — `on_session_start`, `on_tick`, `param_grid`, `requires_full_session`). Implementations: `src/strategies/`. Registry: `src/strategies/mod.rs` (`all_strategies()`).

| Strategy | Ver | Type | Key Logic |
|---|---|---|---|
| PoissonKelly | 11 | Jump-based | Poisson jump counting, Kelly sizing, edge-based entry |
| PoissonKellyDynamic | 7 | Adaptive | Vol-scaled jump_bps, trend bias, dynamic thresholds |
| DriftSniper | 9 | Momentum | OLS velocity confirmation, 50%+ elapsed |
| GapFader | 7 | Mean reversion | Dip entry at 0.50, TP 45% / SL 35%, vol boost |
| RegimeSniper | 7 | Drift + filter | Consistency window filter, sliding max_price |
| TrendDrift | 2 | Regime-gated | Medium/High vol + Trending only, late entry |
| MeanRevFader | 2 | Regime-gated | Flat trend only, small take-profits (15%) |
| LateSniperCB | 1 | Regime-gated + CB | Medium/High vol, late entry, circuit breaker |

**Versioning:** `version` field (simple integers). Bump on logic changes, once per editing session.

## Usage

```bash
cargo build --release

# Backtest all strategies on 30 recent sessions
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy all --top 10 --recent 30

# Single strategy with param overrides
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy PoissonKelly --recent 30 \
  --param min_elapsed=0.4,0.5,0.6 --param stop_loss_pct=0.05,0.10

# Full param sweep with screening
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy DriftSniper --sweep --screen 50 --recent 30

# Compound mode
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy DriftSniper --recent 700 \
  --cash 100 --session-cash-ratio 0.20:99999
```

### Live Trading

```bash
# Option A: use orchestrator (manages everything)
./target/release/orchestrator

# Option B: run processes independently
./target/release/collector --slug-prefix btc-updown-5m-
./target/release/bot-worker --slug-prefix btc-updown-5m- --strategy PoissonKelly --cash 100

# Dashboard (always independent)
cd dashboard && npm run dev
```

### Per-Session Cash Limits

`--session-cash-ratio R:M` (typical): deploy `min(R × bankroll, M)` per session. Scales with wins/losses.

Example: `--cash 20 --session-cash-ratio 0.25:3` → deploys 25% up to $3.

## Technical Reference

**Tick scale:** ~18 ticks/sec. 5-min ≈ 5,400 ticks. BTC variance per tick ~0.01-0.15 bps.

**CLOB constraints:** Min 5 tokens, tick 0.01, range 0.01-0.99, neg_risk=False, spread typically 0.01/0.99. Buy sizes floored to int; sell sizes floor to 4 decimals.

**API latency:** CLOB ~300ms, Gamma ~270ms, Data ~375ms, Builder ~675ms. Negligible for 5-min windows.

**Taker fees (crypto markets):** `fee = tokens × price × 0.25 × (price × (1-price))^2`. Max 1.56% at p=0.50. Constants in `src/defaults.rs`, computed by `compute_taker_fee()`. Backtest: fees on by default, `--no-fees` to disable.

**Guardrails:** Min order 5 tokens. Per-session risk via `--session-cash-ratio`. Settlement cooldown in `Trader::process_tick()`.

**Flatline detection:** `FlatlineDetector` (`src/trader.rs`) monitors up/down midpoints and suppresses all trades when both are unchanged for 15 seconds (`FLATLINE_THRESHOLD_SECONDS` in `src/defaults.rs`), indicating a stale CLOB API. Trading resumes automatically when midpoints move again. Strategy `on_tick` continues running during flatline to keep state consistent. Triggers a `clob_down` notification.

## Data Directory

```
data/
├── app.json                                orchestrator port, PID
├── markets/
│   ├── btc-updown-5m/
│   │   ├── sessions/
│   │   │   ├── 2026-03-01T12-00-00Z.json           full session
│   │   │   ├── 2026-03-01T12-00-00Z.summary.json   lightweight sidecar
│   │   │   └── *.trading.{strategy}.json            trade sidecar
│   │   ├── live/session.json                        current session status
│   │   ├── bot_positions/{Strategy}.json             worker state
│   │   ├── bot_trades.jsonl                         trades (append-only)
│   │   ├── bot_log.jsonl                            session results
│   │   ├── collector_state.json                     collector status
│   │   └── tick.sock                                Unix socket (collector → workers)
│   └── btc-updown-15m/                              (same structure)
└── optimizer/config.json                            sweep configuration
```

## Regime Detection & Gap Handling

### Data Gap Handling

Session data can have gaps (hours of missing sessions due to downtime, API issues, etc.). The system detects and handles these:

- **`gap_before_seconds`**: Attached to each session's metadata during backtesting. The time since the previous session. Large gaps (>600s for 5-min markets) indicate stale data.
- **`data_density`**: Fraction of expected sessions actually present in the lookback window. Below 80% means the lookback data has significant holes.
- **`SessionStatsCache::prior_contiguous()`**: Returns prior sessions stopping at the first large gap.
- **`SessionStatsCache::data_density()`**: Computes how many sessions exist vs how many should exist based on the time span.

Gap threshold is market-specific: each market's `max_session_gap_seconds` (defined in `src/markets.rs`) defaults to 2x its window duration (600s for 5-min, 1800s for 15-min).

### Regime Classification

`src/volatility.rs` classifies the market regime from prior session data:

- **Volatility regime** (`classify_vol_regime`): `Low` (<3 bps mean abs return), `Medium` (3-8 bps), `High` (>8 bps).
- **Trend regime** (`classify_trend_regime`): Uses lag-1 autocorrelation. `Trending` (>0.15), `MeanReverting` (<-0.15), `Flat` (near zero).
- **Data quality**: `clean` or `stale` based on gap and density thresholds.
- **`classify_regime()`**: Combined classifier returning all three dimensions.

### Regime-Gated Strategies

Regime-gated strategies check the regime at session start and only activate when conditions match:

| Strategy | Vol Gate | Trend Gate | Entry Logic |
|---|---|---|---|
| `TrendDrift` | Medium, High | Trending | BTC drift + velocity confirmation |
| `MeanRevFader` | any | Flat | Buy dips below threshold, sell on TP/SL |
| `LateSniperCB` | Medium, High | any | Late-session confidence + circuit breaker |

Regime thresholds are **NOT tunable** — fixed per strategy to prevent overfitting the gate.

## Backtesting & Optimization

**Two-phase backtest: Sharpe for edge, Kelly for sizing.**

- **Phase 1 (Strategy Selection):** Grid search with flat cash per session (default) or compound mode (`--session-cash-ratio`). Ranks by Sharpe.
- **Phase 2 (Kelly Sizing):** Computes `f* = argmax_f Σ log(1 + f·r_i)`. Applies fractional Kelly (default half-Kelly) to get `cash_pct`.

**Metrics:**
- **Entry%** (`entry_rate`): fraction of sessions where the strategy traded. Regime-gated strategies may only enter 2-5%.
- **Win%** (`win_rate`): fraction of *traded* sessions with positive PnL. Sitting out is not a loss.

**Why half-Kelly:** Full empirical Kelly is upward-biased from estimation error. Half-Kelly gives ~75% of growth with much less variance. Use `--kelly-fraction 0.25` for conservative, `1.0` for aggressive.

**Laplace smoothing (Rule of Three):** When no losses observed, inject `max(1, round(3*n/(n+3)))` synthetic total-loss events. Prevents dangerously overfit Kelly sizing.

### Performance

Backtesting uses rayon for automatic parallelism across all cores.

**Two-stage screening (`--screen N`):** For large grids, screens on 1/3 of sessions first, keeps top N, validates on full set. 3-5x speedup.

**Session summary sidecars:** `.summary.json` files (~100 bytes) written alongside full session JSONs. `load_session_summaries()` reads these instead of parsing multi-MB session files, generated automatically on first backtest run.

## Notifications

Configured in `settings.json`. Two mediums: **desktop** (macOS Notification Center via `notify-rust`) and **ntfy** (mobile push via [ntfy.sh](https://ntfy.sh)). Each process loads its own `NotificationManager` — no central service.

Events are hierarchical — subscribing to a parent catches all children:

| Event | Description | ntfy Priority |
|---|---|---|
| `trades` (`trades:buys`, `trades:sells`) | Trade executions | default |
| `session:end` (`session:end:win`, `session:end:loss`) | Session resolution | default / high (loss) |
| `error` | Trade failures, process errors | urgent |
| `process:start`, `process:stop` | Lifecycle events | default / high (stop) |
| `clob_down` | Flat line detector triggered (midpoints stale) | urgent |

See `NOTIFICATIONS.md` for setup instructions and priority overrides.

## Claim Server

The claim server (`claim-server/`) runs as a separate long-lived process that periodically redeems resolved positions via the Builder Relayer. Batches on a 15-minute interval to stay within rate limits (~100/day).

```bash
cd claim-server
uv sync
uv run python main.py
```
