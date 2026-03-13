# REGIMES.md

Quick reference for regime signals and suggested strategy behavior.

## Core Idea
Use recent-session features as priors to select **mode** first, then run a mode-specific strategy.

---

## Strong Regime Signals Found

### 1) Low-move regime (next session likely quiet)
- `W=1`: `vol_mean <= 1.82697e-05` → lower chance of high abs move next session
- `W=5`: `range_max <= 0.00375927` → lower chance of high abs move next session

**Mode mapping:** `QUIET`
- Use fade/mean-reversion behavior
- Smaller sizing
- Tighter stops

---

### 2) Vol-persistence regime (next session likely to move)
- `W=3`: `vol_max >= 3.28199e-05` → much higher chance of high abs move
- `W=5`: `range_min >= 0.00248672` → much higher chance of high abs move
- `W=6`: `range_mean >= 0.00392654` → much higher chance of high abs move

**Mode mapping:** `VOL_PERSIST`
- Use breakout/trend-follow behavior
- Allow larger sizing
- Wider stop/hold logic

---

### 3) Directional up bias
- `W=1`: `crosses >= 16` → higher `P(next_up)`
- `W=2/3`: `crosses_trend` high (e.g., >=15/16) → higher `P(next_up)`

**Mode mapping:** `BIAS_UP`
- Prefer long-up entries
- Raise threshold for down entries or disable down entries

---

### 4) Directional down / anti-up bias
- `W=6`: `crosses_max <= 15` → lower `P(next_up)`

**Mode mapping:** `BIAS_DOWN`
- Prefer down entries
- Require stronger confirmation for up entries

---

### 5) Low-chop vs high-chop proxy
- `W=5`: `above_max >= 0.997` → lower chance of next session being cross-heavy

**Interpretation:** very one-sided recent midpoint behavior often precedes less crossing noise.

**Mode effect:** can favor single-entry conviction behavior over multi-reentry scalping.

---

## Suggested Priority Stack for Mode Selection

1. If strong vol-persistence signals hit → `VOL_PERSIST`
2. Else if strong low-move signals hit → `QUIET`
3. Else apply directional bias (`BIAS_UP` / `BIAS_DOWN`)
4. Else `NEUTRAL`

---

## Notes
- These are exploratory empirical mappings, not guaranteed alpha.
- Use minimum sample-size guards and periodic revalidation.
- Optimize net PnL after friction, not win rate alone.
