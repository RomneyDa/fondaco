# Process Separation Plan

## Problem

Data collection and trading strategies run in the same process (`Runner`). To edit, add, or restart a strategy you must kill the entire process, which stops data collection. Data collection should be always-on and independent.

## Current Architecture

```
Single Process (Runner)
├── Data collection (poll loop, price fetcher, RTDS WebSocket)
├── Strategy slots (StrategySlot[], each with Position + Trader)
├── HTTP control server (port 8420)
└── Session recorder
```

Everything runs in `Runner.run()` → `_run_one_session()`. The poll loop (`_poll_with_trading` or `_poll_collect_only`) does both sampling AND strategy ticks in the same thread. Strategies are added/removed via HTTP but only take effect at session boundaries, and the entire process must restart to pick up code changes.

## Proposed Architecture: Supervisor + Worker Processes

```
Supervisor Process (always running, one per slug)
├── HTTP Control Server (port 8420) — manages workers
├── Data Collector (in-process, never stops)
│   ├── Price fetcher + RTDS WebSocket
│   ├── Session recorder
│   └── Publishes tick data via shared memory / Unix socket / IPC
│
└── Bot Worker Processes (one per strategy, spawned/killed independently)
    ├── Bot Worker: LateSniper (PID 1234)
    ├── Bot Worker: BuyTrend (PID 1235)
    └── Bot Worker: DigitalOption (PID 1236)
```

### Why this design

1. **Data stream never dies** — the supervisor owns data collection in-process. Bot crashes/restarts don't affect it.
2. **Per-strategy isolation** — each bot is a separate process. Kill one, the others keep running. Code changes to a strategy file only require restarting that one bot.
3. **Hot reload** — `trade-on` spawns a new worker process (picks up latest code). `trade-off` kills it. No session-boundary delays.
4. **One supervisor per slug** — `btc-updown-5m-` gets its own supervisor. Could run `eth-updown-15m-` as a separate supervisor on a different port.

## IPC: How bots get tick data

**Option A: Unix domain socket (recommended)**
Supervisor publishes each tick as a JSON line to a Unix socket (`/tmp/polymarket-{slug}.sock`). Bot workers connect and read ticks. Simple, low-latency, no dependencies.

**Option B: Shared memory + event**
Supervisor writes tick data to `multiprocessing.shared_memory`. Workers poll or use an `Event`. More complex, marginal latency benefit for our ~18 ticks/sec rate.

**Option C: File-based (simplest fallback)**
Supervisor writes `data/live_session.json` (already does this). Workers read it. Higher latency (~2s), already partially implemented. Could work as a first step.

**Recommendation: Option A (Unix socket).** It's simple, fast, and naturally supports multiple consumers. Each tick is a JSON line with the same `sample_dict` + `metadata_dict` that `_poll_with_trading` already constructs.

## Implementation Plan

### Phase 1: Extract data collector from Runner (refactor, no behavior change)

Split `Runner` into two classes:
- **`DataCollector`** — owns the poll loop, price fetcher, RTDS client, session recorder. Produces ticks.
- **`Runner`** (slimmed) — still orchestrates, but delegates data collection to `DataCollector`.

This is a pure refactor. The single-process mode still works exactly as before. Tests pass.

### Phase 2: Add tick broadcasting via Unix socket

Add a `TickBroadcaster` to `DataCollector`:
- Opens a Unix domain socket at `/tmp/polymarket-{slug}.sock`
- Accepts multiple client connections
- On each tick, writes a JSON line to all connected clients
- Includes: `sample_dict`, `metadata_dict`, `window_ts`, `window_end_ts`, `tick_index`, `session_id`

Add a `TickSubscriber` that bot workers use to receive ticks:
- Connects to the Unix socket
- Yields tick dicts as they arrive
- Reconnects on disconnect (supervisor restart)

### Phase 3: Bot worker process

Create `app/bot_worker.py`:
- A standalone process that connects to the tick stream and runs ONE strategy
- Receives strategy name, params, cash config via CLI args or a small JSON config
- Uses `TickSubscriber` to get ticks
- Runs `Trader.process_tick()` on each tick (same shared `core/` logic)
- Handles its own `ClobExecutor` (or `SimExecutor` for dry-run)
- Writes trades to `data/bot_trades.jsonl` (same as today)
- Logs to `data/bots/{strategy_name}.log`
- On session boundary (detected from tick stream): resolves, resets position, saves state

Lifecycle:
- Spawned by supervisor via `subprocess.Popen` (or `multiprocessing.Process`)
- Supervisor tracks PID, can kill/restart
- Worker saves its own state to `data/bots/{strategy_name}.json` (cash, position, params)
- On crash: supervisor can auto-restart (optional, configurable)

### Phase 4: Supervisor process management

Update the HTTP control server to manage workers:

- `trade-on --strategy X` → supervisor spawns a new bot worker process
- `trade-off --strategy X` → supervisor sends SIGTERM to that worker
- `status` → supervisor reports data collector status + all worker PIDs/states
- `kill` → supervisor stops all workers, then stops data collector
- `stop` → immediate stop of everything

Supervisor tracks workers in `data/workers.json`:
```json
[
  {"strategy": "LateSniper", "pid": 1234, "params": {...}, "cash": 50.0, "started_at": "..."}
]
```

### Phase 5: Hot code reload

Since each bot is a separate process, restarting it picks up any code changes:
- `trade-off --strategy X && trade-on --strategy X --restore` does a full reload
- Add a convenience command: `uv run python -m app reload --strategy X` that does both atomically
- Could also add file watching (watchdog) for auto-reload on `.py` changes, but that's optional/later

## CLI Changes

```bash
# Unchanged
uv run python -m app start btc-updown-5m-          # starts supervisor + data collector
uv run python -m app trade-on --strategy LateSniper --cash 20
uv run python -m app trade-off --strategy LateSniper
uv run python -m app status
uv run python -m app stop
uv run python -m app kill

# New
uv run python -m app reload --strategy LateSniper   # restart worker, pick up code changes
uv run python -m app logs --strategy LateSniper      # tail worker logs (optional)
```

## Migration Path

- Phase 1-2 can be done without breaking the existing single-process mode
- Phase 3-4 is the real switchover. The old `_poll_with_trading` in Runner gets replaced by worker spawning
- `--restore` still works (reads `data/strategies.json`, spawns workers for each)
- Simulator/backtest is unaffected (uses `SimExecutor` directly, no process separation needed)

## Risk Mitigation

- **Shared ClobExecutor**: Each worker gets its OWN `ClobExecutor` instance (same API key, separate HTTP sessions). No shared mutable state across processes.
- **Trade logging**: Workers append to `bot_trades.jsonl` with file locking (`fcntl.flock`) to avoid interleaving.
- **Balance contention**: Multiple live strategies sharing one USDC balance is already the case today. No change. `--session-cap` limits per-strategy risk.
- **Crash recovery**: Worker saves state to disk on each session boundary. On restart, reads last state. Same as current `--restore`.

## What stays the same

- All `core/` logic (strategies, Position, Trader, Executor) — untouched
- Simulator/backtest — untouched
- Dashboard — reads from same `live_status.json` / `live_session.json` (supervisor writes these)
- Session recording — supervisor still saves session JSONs
- CLAUDE.md constraint: "all trades flow through ClobExecutor.execute_trade" — still true, just in worker processes now
