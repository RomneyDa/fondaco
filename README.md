# Eros PM

Polymarket up/down trading system. See `CLAUDE.md` for full architecture and usage docs.

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
