# Archived Strategies

## May have promise

### MomentumEntry

Buys in the direction BTC is moving if it exceeds a bps threshold, confirmed by token midpoint. Has stop-loss exit logic.

**Best params (2026-03-10, 30 sessions):**
- `entry_window=0.85`, `price_threshold_bps=14`, `midpoint_confirm=0.50`, `stop_loss_pct=0.10`, `bet_fraction=0.5`

**Results:**
- Sharpe: 0.238
- Total PnL: $65.79 / 30 sessions ($2.19/session mean)
- Win rate: 26.7% (traded ~8/30 sessions)
- Geo return: 1.83%
- Kelly (half): 50% cash per session, projects $100 -> $134.82 over 30 sessions, 7.4% max DD

**Why archived:** Edge is thin. Buys tokens after BTC has already moved, so entry price is typically above 0.50. Paying up for tokens that already price in the move. Strategies that buy cheap near 0.50 (LateSniper, PoissonKelly) have better risk/reward profiles.

**Revisit if:** Market structure changes to reward mid-session momentum plays, or if combined with a cheaper entry mechanism.
