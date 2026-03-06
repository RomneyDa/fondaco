# Go Port of Polymarket Trading System

Complete Go port of the Python trading bot. All Python code remains untouched.

## Prerequisites

- Go 1.22+
- The project's `data/` directory (session files, configs) is shared with the Python version

## Build

```bash
cd go
make deps   # download dependencies + tidy go.mod
make build  # builds bin/app, bin/backtest, bin/worker
```

## Usage

Same CLI interface as the Python version:

```bash
# Backtest
./bin/backtest btc-updown-5m --strategy all --top 10 --recent 30

# App lifecycle
./bin/app start
./bin/app start --restore
./bin/app market-on btc-updown-5m-
./bin/app trade-on btc-updown-5m- --strategy LateSniper --cash 100
./bin/app status
./bin/app stop
```

## Architecture

```
cmd/
  app/main.go          CLI entry point (same commands as Python)
  backtest/main.go     Backtest CLI
  worker/main.go       Bot worker binary
internal/
  core/                Shared types, trading logic, network clients
  strategy/            All 13 strategies + registry
  app/                 AppManager, MarketRunner, ControlServer, BotWorker
  simulator/           Backtest engine
```

## Stubs

- `internal/app/auth.go` - CLOB authentication (requires go-ethereum for EIP-712 signing)
- `internal/app/claim.go` - Position redemption (requires go-ethereum for ABI encoding)

These are functional stubs. Live trading requires implementing the go-ethereum integration.
Backtesting and dry-run mode work without them.

## Compatibility

- Reads the same session JSON files as Python
- Writes to the same `data/` directory structure
- HTTP control API is identical (same endpoints, same JSON format)
- Dashboard works unchanged
