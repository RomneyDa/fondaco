# Polymarket Up/Down Trading System

Trade crypto up/down prediction markets on Polymarket. Built for the **5-minute BTC up/down market** (`btc-updown-5m-`). See `README.md` for full architecture, usage, file structure, and technical reference.

## The Market

New market every 5 min. **Up** and **Down** tokens — winner pays $1, loser $0 (Chainlink oracle). Book is thin (best_bid=0.01, best_ask=0.99). The **midpoint** is the real signal (~0.50 at open → ~0.99 on winning side). Edge comes from picking direction, not better fills.

## CRITICAL RULES — REAL MONEY

1. **All trades through `ClobExecutor.execute_trade`** — no ad-hoc order scripts
2. **Read before modifying** execution code (`execute_trade`, `compute_trade_size`, `apply_trade`, `resolve`, `process_tick`)
3. **Position in sync** — `apply_trade()` only after successful CLOB order
4. **Private keys sacred** — `.env` via `python-dotenv`, never logged/committed
5. **Log everything** — trades, sessions, API calls, backtests all to `data/`
6. **When in doubt, do not trade**

## Rules

- **Trading logic in `core/` only** — never add trading behavior only in `app/`. Simulator and bot share all core logic via `core/`.
- **Bump strategy version** on logic changes (once per editing session)
- **`--recent 30`** for coarse grid sweeps, **`--recent 500`** for final validation
- **uv** for packages (`uv sync`, `uv add`, `uv run python -m`)
- **Python >=3.10** — use `typing.Optional`/`typing.Union`, not `X | Y`
- **All HTTP through `LoggingSession`** — no bare `requests.get()`
- **Auth:** `.env` has `POLYMARKET_PRIVATE_KEY`, derives Safe via Polygon RPC, caches in `.cache/auth.json`
- **Strategies:** base class at `core/strategies/base.py`, implementations at `bots/updown/`, registries at `bots/__init__.py`. Import from `bots`, not `core.strategies`.
- **`requires_full_session = True`** when strategy accumulates state from tick 0 (jump counts, warmup windows, running sums)

## Quick Reference

**CLI commands:** `uv run python -m app <command>` / `uv run python -m simulator.backtest`
- App: `start [--restore]`, `stop`, `kill`, `status`
- Markets: `market-on`, `market-off [--after-session]`
- Bots: `trade-on`, `trade-off`, `reload`, `set-cash` (require `--strategy` and slug prefix)
- Optimizer: `optimizer-on`, `optimizer-off`, `optimizer-config`
- Backtest: `uv run python -m simulator.backtest btc-updown-5m --strategy <name> --recent 30`

**Per-session cash limits:** `--session-cash-ratio R:M` deploys `min(R × bankroll, M)` per session (e.g. `--cash 20 --session-cash-ratio 0.25:3` → 25% up to $3). Update live with `set-cash`. Alternatives: `--session-cap N` (fixed), `--per-session` (reset each session).

## Strategy Pitfalls

Common bugs found across strategies. Avoid these when writing or modifying any strategy.

- **Stuck state on expired buy orders.** If a limit buy expires without filling, the strategy must detect that `_entry_midpoint > 0` but `_held_side(ctx) is None` and reset entry state. Otherwise the strategy freezes — thinks it entered, never re-enters, never sells. Always add: `if self._entry_midpoint > 0: self._entry_midpoint = 0.0` in the "no position" branch.
- **Use `_entry_count` instead of `_entered` flags.** Boolean flags like `_entered = True` can't distinguish "order pending" from "order filled" from "order expired." Use `_entry_count` (increment on buy attempt) and check actual position via `_held_side(ctx)`.
- **All limit orders need `expiration_seconds`.** `TradeAction` defaults to 30s GTD. For 5-min markets, use 10s on buys/sells, 5s on very late entries (LateSniperSafe). Without expiry, orders hang on the CLOB indefinitely if the market moves away.
- **Use `_held_side()` and `_has_position()` from base class.** Don't reimplement — they're on `Strategy` in `core/strategies/base.py`.
- **Add `no_new_after` guard.** Strategies that can enter late should stop new entries at ~95% elapsed. The CLOB orderbook gets deleted before the window officially ends — late entries hit "orderbook does not exist" errors.
- **Sell retry: use tick-based cooldown, not one-shot flags.** One-shot (`_exit_sell_attempted = True`) abandons tokens if the sell fails. Throttled retry (`if ctx.tick_index - self._last_sell_tick < 36: return HOLD`) retries on new ticks at updated prices.

## Backtest Speed & Iterative Optimization

Time = variants × sessions × ticks. **Reduce variants first** — that's the biggest lever.

- **`--screen 50`** for large grids (>500 variants): screens on 1/3 of sessions, validates top 50 on full set. 3-5x faster.
- **Iterative refinement, not giant grids:** Coarse (full `param_grid()` + `--screen 50`) → Refine (`--param` with 2-3 values near winner) → Polish (stop when top results cluster within Sharpe 0.05)
- **Independent params → sweep one at a time.** If params don't interact, no grid needed — just vary one while holding others at sensible defaults. A 6-param grid with 4 values each is 4,096 combos; sweeping one-at-a-time is 6×4 = 24.
- **Keep `param_grid()` lean:** 3-4 values per param max. Test `None` (optional features) separately before tuning values.
- **One strategy at a time** for targeted work. `--strategy all` only for broad comparison with `--screen`.
- **`--recent 15`** for exploration, **`--recent 30`** for coarse grid sweeps, **`--recent 500`** for final validation. 30 sessions is enough to find promising directions but will overfit — always validate winners on 500+ sessions (~a few days of data) before trusting Sharpe numbers.
