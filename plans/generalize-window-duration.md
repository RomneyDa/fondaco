# Generalize for 5m + 15m Markets

## Context

The 15-minute BTC up/down market (`btc-updown-15m-`) works identically to the 5-minute market but with a 900s window. The codebase is ~90% parameterized already — `window_duration` flows through Runner → Trader → `elapsed_fraction`. Three changes are needed to fully support both windows.

No auto-detection of window from slug prefix. Users pass `--window 900` explicitly for 15m markets (default stays 300).

## Changes

### 1. Add `window_duration` to `TickContext`

**`core/trader.py`**
- Add `window_duration: float = 300.0` field to the `TickContext` dataclass (after `elapsed_fraction`, line ~203)
- In `process_tick()` (~line 1009), pass `window_duration=self._window_duration` when constructing `TickContext`

This lets any strategy access absolute time when needed, without going through metadata hacks.

### 2. Fix DigitalOption hardcoded 300

**`core/strategies/digital_option.py`** (lines 163, 167)
- Replace `ctx.elapsed_fraction * 300.0` → `ctx.elapsed_fraction * ctx.window_duration`
- Replace `(1.0 - ctx.elapsed_fraction) * 300.0` → `(1.0 - ctx.elapsed_fraction) * ctx.window_duration`

### 3. Merge strategies config into app.json

Currently strategies are persisted in a separate `data/strategies.json` for `--restore`. Move this into `data/app.json` under a `"strategies"` field.

**`core/defaults.py`**
- Remove `STRATEGIES_CONFIG_FILE` constant (line 47)

**`app/runner.py`**
- Import `APP_STATE_FILE` from `core.defaults`
- `_save_strategies_config()`: read existing `app.json`, merge in `"strategies": [...]`, write back atomically
- `restore_strategies()`: read `"strategies"` from `app.json` instead of `strategies.json`
- Remove import of `STRATEGIES_CONFIG_FILE`

**`app/__main__.py`**
- `_write_state(pid, port)`: include `"strategies": []` in initial write
- `_remove_state()`: instead of deleting `app.json`, rewrite it with only `{"strategies": [...]}` (preserve strategies for `--restore` after shutdown). Or: read current strategies before deleting, write back just the strategies.
- Remove `STRATEGIES_CONFIG_FILE` import if present

**app.json format after merge:**
```json
{
  "pid": 12345,
  "port": 8420,
  "strategies": [
    {
      "name": "LateSniper",
      "params": {"min_elapsed": 0.7},
      "cash": 100.0,
      "dry_run": false,
      "per_session": false,
      "session_cap": 50.0
    }
  ]
}
```

After shutdown (strategies preserved for --restore):
```json
{
  "strategies": [
    {"name": "LateSniper", "params": {"min_elapsed": 0.7}, "cash": 100.0, ...}
  ]
}
```

## Strategy params for 15m vs 5m

Same strategies work for both windows. Optimal params will differ because:
- 15m has ~16,200 ticks vs 5m's ~5,400 — EWMA spans/lookbacks should be ~3x larger
- Price variance over 15m is higher — z-score/momentum thresholds need recalibration
- `elapsed_fraction` thresholds (e.g., `min_elapsed=0.7`) are already window-agnostic

Workflow: run separate backtests on 15m session data, deploy with different `--params`:
```bash
uv run python -m app start btc-updown-5m-
uv run python -m app trade-on --strategy DigitalOption --cash 10 --params sigma_span=100

uv run python -m app start btc-updown-15m- --window 900
uv run python -m app trade-on --strategy DigitalOption --cash 10 --params sigma_span=300
```

## Files Modified

| File | Change |
|------|--------|
| `core/trader.py` | Add `window_duration` to `TickContext`, pass it in `process_tick` |
| `core/strategies/digital_option.py` | Use `ctx.window_duration` instead of hardcoded `300.0` |
| `core/defaults.py` | Remove `STRATEGIES_CONFIG_FILE` |
| `app/runner.py` | Rewrite save/restore to use `app.json` instead of `strategies.json` |
| `app/__main__.py` | Update `_write_state`/`_remove_state`, remove stale import |

## Verification

1. Run backtest to confirm TickContext change doesn't break anything:
   ```bash
   uv run python -m simulator.backtest data/sessions/btc-updown-5m --strategy DigitalOption --top 5 --recent 10
   ```
2. Check app.json round-trip: start app → trade-on → verify app.json has strategies → kill → restart with --restore → verify strategies restored
