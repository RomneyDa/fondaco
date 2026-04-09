<p align="center">
  <img src="icon.svg" alt="Fondaco" width="128" height="128" />
</p>

# Fondaco

An open-source trading framework for binary outcome markets — strategies, backtesting, live execution, and a real-time dashboard. Built in Rust for speed.

Currently targets **Polymarket** crypto up/down prediction markets, with support for other binary market platforms planned. Supports multiple market durations simultaneously (e.g. `btc-updown-5m-` and `btc-updown-15m-`).

> **WARNING — Please read before using this software.**
>
> Trading on prediction markets is **essentially gambling**. You can and likely will lose money. This project exists for **fun, education, and exploration** — not as a path to profit.
>
> - **The example strategies included in this repo have not been proven effective.** The author does not use them and makes no claims about their profitability. They exist solely to demonstrate the framework's API.
> - **Dry-run and backtest first.** Use the backtester extensively before even considering live execution. When you do go live, start with tiny amounts you can afford to lose entirely.
> - **You are solely responsible for any financial losses.** This software is provided as-is under the Apache 2.0 license with no warranty. The authors and contributors are not liable for any losses incurred through its use.
> - **Understand what you're running.** This bot places real orders with real money on real markets. Review the code, understand the risks, and proceed at your own discretion.

> **A note on code quality:** This project was built almost entirely with the help of AI (Claude). I am not a skilled Rust engineer — expect rough edges, non-idiomatic patterns, and possibly baffling design decisions throughout. PRs and feedback welcome.

## Quick Start

### Prerequisites

- [Rust](https://rustup.rs/) (stable)
- [Node.js](https://nodejs.org/) 18+ (for the dashboard)
- [Python](https://www.python.org/) 3.10+ with [uv](https://docs.astral.sh/uv/) (for clob-bridge and claim-server)
- A Polymarket account with API credentials

### 1. Clone and build

```bash
git clone https://github.com/RomneyDa/fondaco.git
cd fondaco
cargo build --release
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your Polymarket API credentials:
#   POLYMARKET_PRIVATE_KEY=
#   POLYMARKET_BUILDER_API_KEY=
#   POLYMARKET_BUILDER_SECRET=
#   POLYMARKET_BUILDER_PASSPHRASE=
```

### 3. Set up the CLOB bridge (order signing)

```bash
cd clob-bridge
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py  # runs on :8421
```

### 4. Collect data

```bash
# Start a collector for 5-minute BTC markets
./target/release/collector --slug-prefix btc-updown-5m-
```

Let it run for a while to accumulate session data for backtesting.

### 5. Backtest

```bash
# Run all example strategies on 30 recent sessions
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy all --top 10 --recent 30

# Sweep parameters for a single strategy
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy DriftSniper --sweep --recent 30
```

### 6. Dashboard

```bash
cd dashboard
npm install
npm run dev  # runs on :3456
```

### 7. Live trading (use extreme caution)

```bash
# Option A: orchestrator manages everything
./target/release/orchestrator

# Option B: run processes independently
./target/release/collector --slug-prefix btc-updown-5m-
./target/release/bot-worker --slug-prefix btc-updown-5m- --strategy PoissonKelly --cash 5
```

## The Market

New market every 5 min. **Up** and **Down** tokens — winner pays $1, loser $0 (Chainlink oracle). Book is thin: best_bid=0.01, best_ask=0.99 on both tokens. The **midpoint** (market maker activity) is the real signal (~0.50 at open -> ~0.99 on winning side). Edge comes from **picking direction**, not better fills. Early: midpoint noisy; late (3-4 min): trends strongly; 4.5+ min: losing side's book vanishes.

## Architecture

All core logic (strategies, position math, simulation, backtesting) is in Rust. Live execution uses a collector->worker architecture with Unix socket IPC. CLOB order signing is in Python (`clob-bridge/`).

See the **`/architecture`** page in the dashboard for an interactive visual reference.

```
src/                        Rust core library
  lib.rs                    Crate root
  defaults.rs               Global constants (API URLs, precision, fees, paths)
  markets.rs                Market-specific constants (window duration, gap thresholds)
  models.rs                 Sample, MarketMetadata, SessionData, SessionSummary
  trader.rs                 TradeAction, Position, TickContext, Trade, SimExecutor, Trader
  strategy.rs               Strategy trait, Param, ParamValue
  strategies/               Example strategy implementations + registry
  engine.rs                 Simulator, session loading, Kelly sizing, backtest results
  tick_broadcast.rs         TickBroadcaster/TickSubscriber (Unix socket IPC)
  session_cache.rs          SessionStatsCache (bisect-based lookback)
  session_io.rs             Session JSON + .summary.json sidecar I/O
  rtds.rs                   RTDS WebSocket client (Chainlink BTC price)
  price_fetcher.rs          Sample collection (RTDS -> Coinbase -> CoinGecko fallback)
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
  |-- manages clob-bridge :8421          |-- Next.js 15 App Router + tRPC
  |-- spawns collectors + workers        |-- Express SSE file-watching
  +-- HTTP control API                   +-- proxies to orchestrator or direct file reads

Collector (1 per market)               Bot Worker (N per market)
  |-- RTDS WebSocket + CLOB polling      |-- TickSubscriber (reads tick.sock)
  |-- SessionStatsCache                  |-- strategy.on_tick() -> TradeAction
  |-- writes sessions/ + .summary.json   |-- ClobExecutor -> clob-bridge HTTP
  +-- tick.sock broadcast                +-- writes bot_trades.jsonl
```

Collectors and bot-workers are **fully independent** — the orchestrator is optional convenience for lifecycle management.

### Example Strategies

The strategies in `src/strategies/` are **examples only** — provided to demonstrate the framework's `Strategy` trait. There is no evidence that any of them are profitable.

Your own strategies added to `src/strategies/` are **gitignored by default** so they stay private. Only the bundled examples are tracked.

Trait: `src/strategy.rs` (`Strategy` — `on_session_start`, `on_tick`, `param_grid`, `requires_full_session`). Registry: `src/strategies/mod.rs` (`all_strategies()`).

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

### Writing Your Own Strategy

1. Create a new file in `src/strategies/` (e.g. `my_strategy.rs`)
2. Implement the `Strategy` trait (see any example for the pattern)
3. Add `pub mod my_strategy;` to `src/strategies/mod.rs`
4. Register it in `all_strategies()` in `mod.rs`
5. Build and backtest: `cargo build --release && ./target/release/backtest data/markets/btc-updown-5m/sessions --strategy MyStrategy --recent 30`

Your strategy file is gitignored automatically — only `mod.rs` changes need to be committed (or kept local).

## Backtesting

```bash
# All strategies, 30 recent sessions
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy all --top 10 --recent 30

# Single strategy with param overrides
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy PoissonKelly --recent 30 \
  --param min_elapsed=0.4,0.5,0.6 --param stop_loss_pct=0.05,0.10

# Full param sweep with screening
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy DriftSniper --sweep --screen 50 --recent 30

# Compound mode (bankroll growth simulation)
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy DriftSniper --recent 700 \
  --cash 100 --session-cash-ratio 0.20:99999
```

**Two-phase backtest: Sharpe for edge, Kelly for sizing.**

- **Phase 1 (Strategy Selection):** Grid search with flat cash per session (default) or compound mode (`--session-cash-ratio`). Ranks by Sharpe.
- **Phase 2 (Kelly Sizing):** Computes `f* = argmax_f Sigma log(1 + f*r_i)`. Applies fractional Kelly (default half-Kelly) to get `cash_pct`.

**Metrics:**
- **Entry%** (`entry_rate`): fraction of sessions where the strategy traded.
- **Win%** (`win_rate`): fraction of *traded* sessions with positive PnL. Sitting out is not a loss.

**Performance:** Rayon parallelism across all cores. `--screen N` for two-stage screening (3-5x speedup on large grids).

### Per-Session Cash Limits

`--session-cash-ratio R:M`: deploy `min(R * bankroll, M)` per session. Scales with wins/losses.

Example: `--cash 20 --session-cash-ratio 0.25:3` deploys 25% of bankroll up to $3 per session.

## Technical Reference

**Tick scale:** ~18 ticks/sec. 5-min = ~5,400 ticks. BTC variance per tick ~0.01-0.15 bps.

**CLOB constraints:** Min 5 tokens, tick 0.01, range 0.01-0.99, neg_risk=False, spread typically 0.01/0.99.

**Taker fees:** `fee = tokens * price * 0.25 * (price * (1-price))^2`. Max 1.56% at p=0.50. Constants in `src/defaults.rs`.

**Flatline detection:** `FlatlineDetector` in `src/trader.rs` suppresses trades when both up/down midpoints are unchanged for 15 seconds, indicating a stale CLOB API. Resumes automatically when midpoints move.

## Data Directory

```
data/
|-- app.json                                orchestrator port, PID
|-- markets/
|   |-- btc-updown-5m/
|   |   |-- sessions/
|   |   |   |-- 2026-03-01T12-00-00Z.json           full session
|   |   |   |-- 2026-03-01T12-00-00Z.summary.json   lightweight sidecar
|   |   |   +-- *.trading.{strategy}.json            trade sidecar
|   |   |-- live/session.json                        current session status
|   |   |-- bot_positions/{Strategy}.json             worker state
|   |   |-- bot_trades.jsonl                         trades (append-only)
|   |   |-- bot_log.jsonl                            session results
|   |   |-- collector_state.json                     collector status
|   |   +-- tick.sock                                Unix socket (collector -> workers)
|   +-- btc-updown-15m/                              (same structure)
+-- optimizer/config.json                            sweep configuration
```

## Regime Detection

`src/volatility.rs` classifies the market regime from prior session data:

- **Volatility:** `Low` (<3 bps), `Medium` (3-8 bps), `High` (>8 bps)
- **Trend:** `Trending` (autocorr >0.15), `MeanReverting` (<-0.15), `Flat` (near zero)
- **Data quality:** `clean` or `stale` based on gap and density thresholds

Regime-gated strategies check the regime at session start and only trade when conditions match. Regime thresholds are fixed per strategy to prevent overfitting.

## Notifications

Configured in `settings.json`. Two mediums: **desktop** (macOS Notification Center) and **ntfy** (mobile push via [ntfy.sh](https://ntfy.sh)). See `NOTIFICATIONS.md` for setup.

## Claim Server

The claim server (`claim-server/`) periodically redeems resolved positions via the Builder Relayer.

```bash
cd claim-server
uv sync
uv run python main.py
```

## License

[Apache 2.0](LICENSE)

---

*Curious about the name? A [fondaco](https://en.wikipedia.org/wiki/Fondaco_dei_Tedeschi) was a Venetian merchant warehouse where foreign traders stored goods, struck deals, and ran their operations — the nerve center of medieval international commerce. [Read more about Venice's fondaco system.](https://educated-traveller.com/2019/04/13/venice-a-city-of-trade-a-city-of-merchants/)*
