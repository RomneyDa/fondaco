# Fondaco — Binary Outcome Trading Framework

Trade crypto up/down prediction markets on Polymarket. Multiple market durations (e.g. `btc-updown-5m-`, `btc-updown-15m-`).

## Architecture

**Rust** (`src/`) — strategies, backtester, position math, volatility, session cache, live collector + bot workers.
**Sports** (`src/sports/`) — discovery, live feeds, fair value, adaptive cadence, per-game storage.
**Dashboard** (`dashboard/`) — Next.js 15 (App Router) + tRPC + Express, port 3456.
**clob-bridge** (`clob-bridge/`) — Python Flask wrapper around py_clob_client, port 8421.
**model-server** (`model-server/`) — Python Dixon-Coles fitting, serves rate params, port 8422.
**claim-server** (`claim-server/`) — Python claim/redeem service, port 8080.

See the `/architecture` page in the dashboard for the full visual reference. **If you change the architecture (new binaries, IPC, modules, ports), update `dashboard/src/components/Architecture.tsx`.**

### Key binaries
- `collector` — 1 per market, RTDS + CLOB polling, writes sessions, broadcasts ticks via Unix socket
- `bot-worker` — 1 per strategy, reads ticks from collector's socket, trades via clob-bridge
- `orchestrator` — optional process manager (:8420), spawns collector/workers/clob-bridge
- `backtest` — offline param sweeps with rayon parallelism
- `sports-collector` — 1 per league, N concurrent games, adaptive cadence, read-only
- `sports-bot-worker` — 1 per strategy per league, owns the allocator + confirmation gate
- `sports-backtest` — overlapping-game simulator, depth-aware fills, CLV/calibration

Collectors and bot-workers run independently — the orchestrator is optional convenience.

## The Market

New market every 5 min. **Up** and **Down** tokens — winner pays $1, loser $0 (Chainlink oracle). Book is thin (best_bid=0.01, best_ask=0.99). The **midpoint** is the real signal (~0.50 at open -> ~0.99 on winning side). Edge comes from picking direction, not better fills.

**Limit orders, not FAK.** Always use limit orders with `expiration_seconds` and `cancel_after_s`.

## CRITICAL RULES — REAL MONEY

1. **All trades through `ClobExecutor`** — no ad-hoc order scripts
2. **Read before modifying** execution code (`execute_trade`, `compute_trade_size`, `apply_trade`, `resolve`, `process_tick`)
3. **Position in sync** — `apply_trade()` only after successful CLOB order
4. **Private keys sacred** — `.env`, never logged/committed
5. **Log everything** — trades, sessions, API calls, backtests all to `data/`
6. **When in doubt, do not trade**

## Sports

**A game is a session.** Legs are indexed: 0=home, 1=away, 2=draw (`trader::leg_index_for_key`).

- **Never trade an unjoined game.** A `MarketGroup` without a `game_id` has no live state behind it. `is_tradeable()` enforces this; an ambiguous team-name join is refused, not guessed.
- **Never trade on fallback model params.** `FairValueSource::ModelFallback` means league averages with no opinion about who is better — an "edge" against them is an artefact. Strategies with `requires_model()` refuse it.
- **Record book depth, always.** The backtest fill model walks recorded levels; depth cannot be backfilled.
- **≤3 tunable params per sports strategy.** 272 NFL games a year cannot support more; a registry test enforces it.
- **Judge on calibration and CLV, not PnL.** PnL has almost no statistical power at sports sample sizes. Walk-forward splits by season, never randomly.
- **Confirmation before action.** A goal can be disallowed and a touchdown reversed. `ConfirmationGate` applies a delay, two-feed agreement, and review suspension.
- **Staleness is not flatline.** Crypto's `FlatlineDetector` treats a frozen book as broken data and suppresses trading. Sports `staleness` treats it as the opportunity — gate it on the book being alive.
- **Selection is flags only, never a config file.** `--team`, `--exclude-team`, `--game`, `--min-start`/`--max-start`, `--market-kind`. `--game` overrides team filters. Bad flag values exit 2 before anything starts.
- **Never fill the concurrency cap by iteration order.** `sessions` is a `HashMap` and Rust randomises its order per process, so first-come gave a different arbitrary subset every restart. Rank with `selection::rank` — `--prefer random` is seeded and reproducible; `edge`/`scheduled`/`liquidity` are deterministic. Open positions always outrank contenders so they can exit.
- **Strategies iterate `ctx.tradeable_legs()`, not `group.moneyline_legs()`**, so `--market-kind` actually reaches spread and total legs.
- **Every rejected game is logged** to `markets/<league>/skipped.jsonl` with a reason.
- **Storage is append-only JSONL.** `write_live_session` rewrites the whole session and is quadratic over a 3-hour game.

## Rules

- **Trading logic in `src/`** — strategies, position math, trade sizing, simulation all live in Rust.
- **Bump strategy version** on logic changes (once per editing session)
- **`--recent 30`** for coarse grid sweeps, **`--recent 500`** for final validation
- **Rust toolchain:** `cargo build --release`, `cargo test`, `cargo run --bin backtest`
- **Strategies:** trait at `src/strategy.rs`, implementations at `src/strategies/`, registry at `src/strategies/mod.rs` (`all_strategies()`).
- **Regime-gated strategies** check vol/trend regime at session start and set `_regime_active`. In `on_tick`, return `hold()` when inactive. Regime thresholds are NOT tunable params.
- **`requires_full_session = true`** when strategy accumulates state from tick 0 (jump counts, warmup windows, running sums)
- **No magic numbers** — global constants in `src/defaults.rs`; market-specific in `src/markets.rs`.
- **Tests must pass after changes.** Run `cargo test` and fix any failures before considering work done.
- **The golden backtest guards the execution path.** `tests/golden_backtest.rs` snapshots per-session PnL and trade tapes for every strategy against synthetic sessions. If it fails, trading behaviour changed. Regenerate only deliberately: `UPDATE_GOLDEN=1 cargo test --test golden_backtest`.
- **Per-market economics.** Fees, tick size and min order size live in `MarketEconomics`, not global constants. Do not reintroduce hardcoded `PRICE_DECIMALS` / `CLOB_MIN_ORDER_TOKENS` in execution paths.
- **Dashboard:** Next.js 15 App Router, tRPC, Tailwind v4. Pages in `dashboard/src/app/`, components in `dashboard/src/components/`, tRPC routers in `dashboard/src/server/routers/`.

## Quick Reference

**Backtest CLI:** `./target/release/backtest <data_dir> [options]`

```bash
# Run all strategies on 30 recent sessions
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy all --recent 30

# Single strategy with param sweep
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy PoissonKelly --sweep --recent 30

# Param overrides
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy DriftSniper --recent 30 \
  --param min_elapsed=0.4,0.5,0.6 --param stop_loss_pct=0.05,0.10

# Compound mode
./target/release/backtest data/markets/btc-updown-5m/sessions --strategy PoissonKelly --recent 700 \
  --cash 100 --session-cash-ratio 0.20:99999
```

**Sports CLI:**

Game selection is **flags only** — there is no persisted config. To change what
a bot trades, kill it and restart with different flags. `--list-games` prints
the slate a command would select and exits, so it is always safe to run first.

```bash
# See what a command would pick, without starting anything
./target/release/sports-collector --league nfl --list-games

# Collect a league (read-only; no trading)
./target/release/sports-collector --league nfl --log-file

# Narrow the slate
./target/release/sports-collector --league nfl --team Chiefs --team Bills
./target/release/sports-collector --league epl --exclude-team "Sheffield"
./target/release/sports-collector --league nfl --game 5127839
./target/release/sports-collector --league nfl --game kc-phi --game "eagles vs"
./target/release/sports-collector --league nfl --min-start 18:00 --max-start 23:59
./target/release/sports-collector --league nfl --market-kind moneyline --market-kind total

# Backtest with season-split walk-forward
./target/release/sports-backtest --league nfl --walk-forward

# Dry-run a strategy live (add --live to trade for real)
./target/release/sports-bot-worker --league epl --strategy ModelEdge --cash 500

# Choose how the concurrency cap is filled when more games are eligible
./target/release/sports-bot-worker --league nfl --strategy ModelEdge --prefer edge
./target/release/sports-bot-worker --league nfl --strategy StaleBook --prefer random --prefer-seed 7

# Fit soccer team strengths from football-data.co.uk CSVs
cd model-server && uv run python main.py --fit-dir ./data/epl --league epl
```

## Collector Management

Collectors run independently — no orchestrator required.

**Start:** `./target/release/collector --slug-prefix btc-updown-5m-`

**Signals:** SIGTERM/SIGINT = stop immediately. SIGUSR1 = finish current session then exit.

**Dashboard API (port 3456):**

```bash
# Start collector
curl -X POST localhost:3456/api/collector/start -H 'Content-Type: application/json' \
  -d '{"slug_prefix": "btc-updown-5m-"}'

# Stop (immediate or after session)
curl -X POST localhost:3456/api/collector/stop -H 'Content-Type: application/json' \
  -d '{"slug_prefix": "btc-updown-5m-", "after_session": true}'

# Restart at next window boundary (rebuild binary from latest code)
curl -X POST localhost:3456/api/collector/restart -H 'Content-Type: application/json' \
  -d '{"slug_prefix": "btc-updown-5m-", "rebuild": true}'
```

## Strategy Pitfalls

- **Stuck state on expired buy orders.** If a limit buy expires, the strategy must detect `entry_midpoint > 0` but `held_side() is None` and reset. Use `check_expired_entry()`.
- **Use `entry_count` instead of boolean flags.**
- **Use `cancel_after_s` for soft cancellation.** Set `cancel_after_s: Some(10.0)` on buy TradeActions.
- **Use `held_side()` and `has_position()` from `strategy.rs`.** Don't reimplement.
- **Add `no_new_after` guard.** Stop new entries at ~95% elapsed.
- **Sell retry: use time-based cooldown.** Throttled retry via elapsed seconds.
- **No tick-based decisions.** Use `ctx.elapsed_seconds` / `ctx.elapsed_fraction`, never `ctx.tick_index`.
- **Circuit breaker.** Track recent outcomes across sessions. Suspend after losses, shadow-pick to validate recovery.

## Notifications

Configured in `settings.json`. Two mediums: **desktop** (macOS Notification Center) and **ntfy** (mobile push via ntfy.sh). Each medium has `enabled` + `events` list. Events are hierarchical (`trades` catches `trades:buys` + `trades:sells`). Full docs in `NOTIFICATIONS.md`.

Event types: `trades`, `trades:buys`, `trades:sells`, `session:end`, `session:end:win`, `session:end:loss`, `error`, `process:start`, `process:stop`, `clob_down`, `report:hourly`.

Each process loads `NotificationManager::load()` independently — no central notification service. Bot workers and orchestrator both send their own notifications.

## Flatline Detection

`FlatlineDetector` (`src/trader.rs`) suppresses trades when both up/down midpoints are unchanged for `FLATLINE_THRESHOLD_SECONDS` (15s, in `src/defaults.rs`). This indicates the CLOB API is stale. Trading resumes automatically as soon as midpoints move again — it's temporary, not session-scoped. The strategy's `on_tick` still runs during flatline so state stays consistent; only trade execution is suppressed. Triggers a `clob_down` notification.

## Latency Simulation

`--latency MS` models network round-trip (~200ms). Strategy decides on tick T's data but order executes at tick T+delay's prices. Cash/tokens reserved immediately. Deferred actions cancelled at session end.

## Backtest Speed

- **`--screen 50`** for large grids: screens on 1/3 of sessions, validates top 50 on full set.
- **Iterative refinement:** Coarse -> Refine -> Polish. Stop when top results cluster within Sharpe 0.05.
- **Rayon parallelism:** Rust backtester uses all cores automatically via rayon.
- **`--recent 15`** for exploration, **`--recent 30`** for sweeps, **`--recent 500`** for validation.

## Backtest Metrics

- **Entry%** (`entry_rate`): fraction of sessions with at least one trade.
- **Win%** (`win_rate`): fraction of *traded* sessions with positive PnL.
- These are distinct: 20/700 sessions traded, 17 won -> Entry=2.9%, Win=85%.
