# Migration Plan: Python → Go

## Why Go (not Rust)

**Go wins for this codebase:**

1. **Concurrency model is a 1:1 fit.** AppManager → MarketRunners → Workers maps directly to goroutines + channels. The current Python code fights the GIL with threads + subprocesses. Go does this natively with goroutines.
2. **Network-heavy, not compute-heavy.** API latency is 300ms+. Bottleneck is I/O, not CPU. Go's HTTP/WebSocket story is best-in-class.
3. **Single binary deployment.** No virtualenv, no `uv sync`, no Python version management. `go build` → deploy. Massive ops win for a trading bot that needs to stay up.
4. **Backtesting gets ~10-50x faster for free.** Python tick-processing loops over 5,400 ticks × hundreds of sessions is the main bottleneck in backtest. Go eats this alive.
5. **Subprocess management** (`os/exec`) is more ergonomic than Python's for managing worker processes.
6. **Ethereum ecosystem exists** — `go-ethereum` has all the signing/ABI primitives needed for CLOB auth.

**Why not Rust:**
- API latency dominates execution time — Rust's extra performance over Go is wasted
- Borrow checker adds friction to the networking/concurrency patterns used here
- 3-5x slower development for negligible runtime benefit
- Smaller ecosystem for Polymarket-specific tooling (CLOB client, etc.)

---

## Dependency Mapping

| Python | Go Equivalent |
|--------|--------------|
| `requests` (HTTP) | `net/http` (stdlib) |
| `websocket-client` | `github.com/gorilla/websocket` |
| `py-clob-client` | Custom (REST + EIP-712 signing) — see Phase 2 |
| `py-builder-relayer-client` | Custom HTTP client (thin wrapper) |
| `eth_account`, `eth_abi`, `eth_utils` | `github.com/ethereum/go-ethereum` (accounts, abi, crypto) |
| `python-dotenv` | `github.com/joho/godotenv` |
| `json` | `encoding/json` (stdlib) |
| `threading` | goroutines + `sync.Mutex` / channels |
| `subprocess` | `os/exec` |
| `argparse` | `github.com/spf13/cobra` or stdlib `flag` |
| `logging` | `log/slog` (stdlib, structured) |
| `dataclass` | structs |
| `abc.ABC` | interfaces |
| `pytest` | `testing` (stdlib) |
| `glob` | `filepath.Glob` (stdlib) |
| Unix sockets (`tick_broadcast.py`) | `net.Listen("unix", path)` (stdlib) |

---

## Architecture in Go

```
cmd/
  app/main.go              # CLI entry point (cobra)
  backtest/main.go         # Backtest CLI
internal/
  core/
    defaults.go            # Constants, path helpers
    models.go              # PriceSample, MarketMetadata, SessionData
    trader.go              # TradeAction, Position, TickContext, Trade, Executor interface, Trader
    sim_executor.go        # SimExecutor
    clob_executor.go       # ClobExecutor (live trading)
    api_logger.go          # LoggingHTTPClient (wraps http.Client)
    session_recorder.go    # Session JSON I/O
    tick_broadcast.go      # Unix socket broadcaster + subscriber
    rtds_client.go         # WebSocket client for Chainlink prices
    market_discovery.go    # Gamma API lookup
    price_fetcher.go       # BTC + CLOB price polling
    window_ops.go          # Window timing, metadata retry, resolution
  strategy/
    strategy.go            # Strategy interface
    registry.go            # UPDOWN_STRATEGIES, MARKET_STRATEGIES, get_strategies_for_market()
    late_sniper.go         # ...one file per strategy
    digital_option.go
    buy_trend.go
    ...
  app/
    manager.go             # AppManager
    market_runner.go       # MarketRunner
    bot_worker.go          # Worker process (separate binary or same binary with subcommand)
    server.go              # HTTP control server
    auth.go                # CLOB auth (Safe derivation, API creds, cache)
    claim.go               # Claim/redeem logic
  simulator/
    engine.go              # Simulator, SessionResult, BacktestResult
    backtest.go            # Grid search runner
```

### Key Design Decisions

**Workers as goroutines, not subprocesses.** Python needed subprocesses to escape the GIL. Go goroutines + channels give isolation without process overhead. Each worker goroutine owns its Position, Executor, and connects to the tick channel. If we still want process isolation for crash resilience, use `os/exec` to spawn `cmd/worker/main.go`.

**Strategy as an interface:**
```go
type Strategy interface {
    Name() string
    OnSessionStart(metadata map[string]any)
    OnTick(ctx TickContext) TradeAction
    ParamGrid() map[string][]any
}
```

**Tick broadcast via channels instead of Unix sockets (for in-process workers):**
```go
// MarketRunner owns the broadcast channel
type MarketRunner struct {
    ticks chan TickMessage
    // workers subscribe by receiving from fan-out channels
}
```
Keep Unix sockets as an option for out-of-process workers or dashboard SSE.

---

## Migration Phases

### Phase 0: Project Setup
- [ ] Initialize Go module: `go mod init github.com/user/polymarket-bot`
- [ ] Set up directory structure under `go/` (parallel to Python, not replacing it yet)
- [ ] Add `Makefile` with `build`, `test`, `lint` targets
- [ ] Set up `golangci-lint` config
- [ ] Port `.env` loading with `godotenv`

### Phase 1: Core Types & Simulator (no network, no auth)
**Goal:** Run backtests in Go. This validates the port of all trading logic.

- [ ] `internal/core/defaults.go` — constants, path helpers
- [ ] `internal/core/models.go` — PriceSample, MarketMetadata, SessionData structs + JSON tags
- [ ] `internal/core/trader.go` — TradeAction, Position, TickContext, Trade, Executor interface, Trader
  - Position math (compute_trade_size, apply_trade, resolve) is the most critical code. Port line-by-line, test exhaustively.
- [ ] `internal/core/sim_executor.go` — SimExecutor (book + slippage modes)
- [ ] `internal/strategy/strategy.go` — Strategy interface
- [ ] `internal/strategy/registry.go` — strategy registry
- [ ] Port 2-3 strategies (start with `LateSniper`, `DigitalOption`, `BuyTrend`)
- [ ] `internal/simulator/engine.go` — Simulator, load_sessions, run_session, run_backtest
- [ ] `cmd/backtest/main.go` — CLI grid search runner
- [ ] **Validation:** Run Go backtest on same session data, compare results with Python output tick-for-tick. Results must be identical (or within floating-point epsilon).

### Phase 2: CLOB Client & Auth
**Goal:** Authenticate and place orders on Polymarket.

- [ ] `internal/app/auth.go` — Safe address derivation via Polygon RPC (go-ethereum ABI encoding)
- [ ] Custom CLOB client (the Python `py-clob-client` is thin — it's just REST + EIP-712 signing):
  - [ ] EIP-712 order signing (go-ethereum `crypto.Sign`)
  - [ ] `POST /order` (create_and_post_order equivalent)
  - [ ] `GET /book` (get_order_book)
  - [ ] `GET /balance` (get_balance_allowance)
  - [ ] `POST /cancel` (cancel order)
  - [ ] API key auth headers (HMAC)
- [ ] `internal/core/clob_executor.go` — ClobExecutor using the Go CLOB client
- [ ] Builder Relayer client for claim/redeem
- [ ] **Validation:** Place a small test order via Go, verify on Polymarket UI.

### Phase 3: Network & Data Collection
**Goal:** Collect live market data.

- [ ] `internal/core/rtds_client.go` — WebSocket client for Chainlink prices (gorilla/websocket)
  - Reconnection loop with backoff
  - Watchdog goroutine
- [ ] `internal/core/api_logger.go` — HTTP client wrapper with request logging
- [ ] `internal/core/market_discovery.go` — Gamma API market lookup
- [ ] `internal/core/price_fetcher.go` — BTC + CLOB price polling
- [ ] `internal/core/window_ops.go` — window timing, metadata retry, resolution polling
- [ ] `internal/core/session_recorder.go` — session JSON I/O (must write same format for backward compat)
- [ ] `internal/core/tick_broadcast.go` — Unix socket broadcaster (for dashboard compat)
- [ ] **Validation:** Run data collection, verify session JSON files match Python output format.

### Phase 4: App Manager & Workers
**Goal:** Full live trading system.

- [ ] `internal/app/manager.go` — AppManager (goroutines for markets)
- [ ] `internal/app/market_runner.go` — MarketRunner (data + worker management)
- [ ] `internal/app/bot_worker.go` — worker (goroutine or subprocess)
- [ ] `internal/app/server.go` — HTTP control server (same API as Python: `/api/control/*`)
- [ ] `internal/app/claim.go` — claim/redeem logic
- [ ] `cmd/app/main.go` — CLI (start, stop, kill, market-on/off, trade-on/off, reload, status, claim)
- [ ] State persistence (`data/app.json`, `data/markets/*/strategies.json`)
- [ ] Worker state files (`state.json`, `live_trades.json`)
- [ ] **Validation:** Run Go app alongside Python dashboard. All HTTP/SSE endpoints must work unchanged.

### Phase 5: Remaining Strategies
- [ ] Port all strategies from `core/strategies/`:
  - late_sniper.py, late_sniper_tp.py, digital_option.py, buy_trend.py
  - volatility_bargain.py, bargain_hunter.py, dip_sniper.py
  - brenda_flipper.py, cheap_shot.py, midpoint_momentum.py
  - momentum_entry.py, fade_the_market.py, god_strategy.py
- [ ] Verify each against Python backtest output

### Phase 6: Cutover & Cleanup
- [ ] Run Go app in shadow mode (collect data, simulate trades, log but don't execute)
- [ ] Compare shadow results with Python live results over 24-48h
- [ ] Switch live trading to Go
- [ ] Move Python code to `legacy/` or delete
- [ ] Update CLAUDE.md, dashboard server config, deployment scripts

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Floating-point divergence in Position math | Port line-by-line, use `math.Floor` matching Python's `math.floor`. Run comparison tests on 100+ sessions. |
| CLOB signing incompatibility | Test with real (small) orders early in Phase 2. The EIP-712 signing must produce identical signatures. |
| Session JSON format change breaks dashboard | Write format-compatibility tests. Dashboard reads JSON — struct tags must match exactly. |
| Missing `py-clob-client` features | The client is thin (REST + signing). Read its source to enumerate all endpoints used. |
| Go-ethereum dependency weight | Only import the specific packages needed (crypto, accounts, abi). Don't pull in the full node. |

## Estimated Scope

| Component | Python LoC (approx) | Complexity |
|-----------|---------------------|------------|
| core/trader.py | ~600 | High — money path |
| core/ (other) | ~800 | Medium |
| strategies/ | ~1200 | Low — mechanical port |
| app/ | ~900 | Medium-High |
| simulator/ | ~400 | Low |
| tests/ | ~2000 | Medium |
| **Total** | **~5900** | |

Go is typically ~1.3-1.5x the LoC of Python for equivalent code. Expect ~7500-9000 lines of Go.

## Order of Operations Summary

```
Phase 0 (setup)           ████
Phase 1 (core + sim)      ████████████████  ← validate here before proceeding
Phase 2 (CLOB + auth)     ████████████
Phase 3 (network + data)  ████████████
Phase 4 (app manager)     ████████████████
Phase 5 (all strategies)  ████████
Phase 6 (cutover)         ████████
```

Phase 1 is the critical gate. If backtest results match Python, the rest is plumbing.
