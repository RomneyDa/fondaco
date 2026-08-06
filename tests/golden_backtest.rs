//! Golden backtest — behavioural snapshot of every registered strategy.
//!
//! This exists to guard the Phase 0 domain generalization (binary `up`/`down`
//! `Position` -> leg-indexed `Position`, stringly-typed sides -> `Side` enum).
//! Those changes touch `execute_trade`, `compute_trade_size`, `apply_trade`,
//! `resolve` and `process_tick` — the execution path CLAUDE.md flags as
//! read-before-modify. `cargo test` alone does not prove the refactor is
//! behaviour-preserving; this does.
//!
//! Sessions are synthesised deterministically (seeded LCG, no `rand`
//! dependency) so the snapshot is reproducible on any machine without needing
//! the private `data/` directory.
//!
//! Regenerate deliberately with:
//!     UPDATE_GOLDEN=1 cargo test --test golden_backtest
//! A diff in `tests/golden/backtest_snapshot.json` that you did not
//! deliberately regenerate means the refactor changed trading behaviour.

use fondaco::engine::{pre_slice_summaries, Simulator};
use fondaco::models::{MarketMetadata, Sample, SessionData, SessionSummary};
use fondaco::strategies::all_strategies;

use std::collections::BTreeMap;
use std::path::PathBuf;

// ---------------------------------------------------------------------------
// Deterministic session synthesis
// ---------------------------------------------------------------------------

/// Minimal LCG (numerical recipes constants). We only need reproducible noise,
/// not statistical quality, and avoiding `rand` keeps the snapshot stable
/// across dependency bumps.
struct Lcg(u64);

impl Lcg {
    fn next_u32(&mut self) -> u32 {
        self.0 = self
            .0
            .wrapping_mul(1_664_525)
            .wrapping_add(1_013_904_223);
        (self.0 >> 16) as u32
    }

    /// Uniform in [0, 1).
    fn next_f64(&mut self) -> f64 {
        self.next_u32() as f64 / (u32::MAX as f64 + 1.0)
    }

    /// Roughly standard normal via sum-of-uniforms (Irwin–Hall, n=12).
    fn next_normal(&mut self) -> f64 {
        let mut acc = 0.0;
        for _ in 0..12 {
            acc += self.next_f64();
        }
        acc - 6.0
    }
}

const WINDOW_DURATION: f64 = 300.0;
const SAMPLE_INTERVAL: f64 = 0.5;
const NUM_SESSIONS: usize = 90;

/// Sessions are generated in volatility blocks so that a strategy's trailing
/// regime lookback lands squarely inside Low, Medium and High buckets at
/// different points in the run (`volatility::VOL_LOW_THRESHOLD_BPS` = 3,
/// `VOL_HIGH_THRESHOLD_BPS` = 8, measured as mean absolute session return in
/// bps). Without this the regime-gated strategies never activate and the
/// snapshot would not guard their execution path.
fn vol_block(index: usize) -> (f64, f64) {
    // (per-step sigma in bps, drift magnitude in bps/sec)
    match index / 30 {
        0 => (0.06, 0.004), // low vol   — session |return| ~ 1-2 bps
        1 => (0.20, 0.012), // medium    — ~ 4-6 bps
        _ => (0.60, 0.030), // high      — ~ 12-18 bps
    }
}

/// Drift sign schedule. Blocks alternate between *runs* of same-sign drift
/// (positive autocorrelation -> `TrendRegime::Trending`) and strict alternation
/// (negative autocorrelation -> `TrendRegime::MeanReverting`), so both branches
/// of every regime gate are reached somewhere in the run.
fn drift_sign(index: usize) -> f64 {
    let trending_block = (index / 30) % 2 == 0;
    if trending_block {
        // Long runs keep the trailing-20 autocorrelation comfortably above
        // volatility::AUTOCORR_TREND_THRESHOLD.
        if (index / 15) % 2 == 0 {
            1.0
        } else {
            -1.0
        }
    } else if index % 2 == 0 {
        1.0
    } else {
        -1.0
    }
}

/// Exponential-moving-average time constant for the book's reaction to BTC, in
/// seconds. Real up/down books do not reprice instantly — that lag is precisely
/// the inefficiency the displacement strategies target, and without modelling it
/// the midpoint is a pure function of the BTC return and strategies that require
/// a *cheap* token on a confirmed move (RegimeSniper's `max_price` = 0.49) can
/// never enter.
const BOOK_LAG_TAU_S: f64 = 60.0;

/// Build one synthetic 5-minute BTC up/down session.
///
/// BTC follows a random walk with a session-level drift so some sessions trend
/// and some chop. Token midpoints are derived from the walk with a logistic
/// map that sharpens as the window elapses, which reproduces the real market's
/// "0.50 at open -> 0.99 on the winning side" shape closely enough to exercise
/// every entry, stop-loss and take-profit branch in the strategies.
fn synth_session(index: usize) -> SessionData {
    let mut rng = Lcg(0x5EED_0000 + index as u64 * 7919);

    let initial_btc = 60_000.0 + (index as f64 * 137.0);
    let (vol_bps, drift_mag) = vol_block(index);

    // In trending blocks the drift must dominate the random walk, otherwise the
    // trailing autocorrelation is swamped by noise and the regime never
    // classifies as Trending. In mean-reverting blocks the magnitude varies so
    // the set still contains strong trends, weak trends and pure chop.
    let trending_block = (index / 30) % 2 == 0;
    let magnitude = if trending_block {
        drift_mag * 2.0
    } else {
        match index % 5 {
            0 => 0.0,
            1 | 2 => drift_mag,
            _ => drift_mag * 0.35,
        }
    };
    let drift_bps_s = magnitude * drift_sign(index);

    let n_samples = (WINDOW_DURATION / SAMPLE_INTERVAL) as usize;
    let mut samples = Vec::with_capacity(n_samples);
    let mut btc = initial_btc;
    // The book's laggy view of the BTC return.
    let mut book_return_bps = 0.0_f64;
    let alpha = (SAMPLE_INTERVAL / BOOK_LAG_TAU_S).min(1.0);

    for i in 0..n_samples {
        let elapsed = i as f64 * SAMPLE_INTERVAL;

        // Random walk in bps, plus deterministic drift.
        let shock_bps = rng.next_normal() * vol_bps;
        let step_bps = drift_bps_s * SAMPLE_INTERVAL + shock_bps;
        btc *= 1.0 + step_bps / 10_000.0;

        let return_bps = (btc - initial_btc) / initial_btc * 10_000.0;

        // The book chases the BTC return rather than tracking it exactly.
        book_return_bps += alpha * (return_bps - book_return_bps);

        // Confidence grows as the window elapses: early moves barely move the
        // book, late moves push it toward resolution.
        let elapsed_frac = elapsed / WINDOW_DURATION;
        let sharpness = 0.03 + 0.35 * elapsed_frac * elapsed_frac;

        // Independent quote noise so the midpoint is not a deterministic
        // function of BTC — the fader strategies key off exactly this.
        let quote_noise = rng.next_normal() * 0.012;
        let up_mid = logistic(book_return_bps * sharpness) + quote_noise;

        // Clamp into the tradeable band and round to the CLOB tick.
        let up_mid = round_to(up_mid.clamp(0.02, 0.98), 2);
        let down_mid = round_to(1.0 - up_mid, 2);

        samples.push(Sample {
            timestamp_utc: Some(format!("2026-01-01T00:00:{:06.3}Z", elapsed.min(59.999))),
            elapsed_seconds: elapsed,
            btc_price_usd: Some(round_to(btc, 2)),
            up_token_midpoint: Some(up_mid),
            down_token_midpoint: Some(down_mid),
            // Real up/down books are near-empty; strategies that read the book
            // (execution_mode = "book") need a plausible ask.
            up_token_best_bid: Some(round_to((up_mid - 0.02).max(0.01), 2)),
            up_token_best_ask: Some(round_to((up_mid + 0.02).min(0.99), 2)),
            down_token_best_bid: Some(round_to((down_mid - 0.02).max(0.01), 2)),
            down_token_best_ask: Some(round_to((down_mid + 0.02).min(0.99), 2)),
            server_timestamp: None,
        });
    }

    let final_btc = btc;
    let outcome = if final_btc > initial_btc { "Up" } else { "Down" };

    // Window starts are spaced one real window apart so `gap_before` and
    // `data_density` land on their "healthy" values.
    let window_start = window_start_utc(index);

    SessionData {
        metadata: MarketMetadata {
            event_id: Some(format!("golden-event-{index}")),
            event_slug: Some(format!("btc-updown-5m-{}", 1_700_000_000 + index * 300)),
            event_title: Some("Golden BTC Up/Down".to_string()),
            condition_id: Some(format!("golden-cond-{index}")),
            up_token_id: Some(format!("golden-up-{index}")),
            down_token_id: Some(format!("golden-down-{index}")),
            window_start_utc: Some(window_start),
            window_end_utc: None,
            resolution_source: Some("chainlink".to_string()),
        },
        initial_btc_price_usd: Some(round_to(initial_btc, 2)),
        final_btc_price_usd: Some(round_to(final_btc, 2)),
        outcome: Some(outcome.to_string()),
        outcome_prices: Some(if outcome == "Up" {
            vec!["1".to_string(), "0".to_string()]
        } else {
            vec!["0".to_string(), "1".to_string()]
        }),
        samples,
        collection_started_utc: String::new(),
        collection_ended_utc: String::new(),
        error_count: 0,
        errors: Vec::new(),
        source_file: format!("golden/session-{index:04}.json"),
        _prior_summaries: None,
    }
}

fn window_start_utc(index: usize) -> String {
    // 2026-01-01T00:00:00Z + index * 5 minutes, formatted by hand so the test
    // has no dependency on chrono's formatting behaviour.
    let total_minutes = index * 5;
    let hours = total_minutes / 60;
    let minutes = total_minutes % 60;
    format!("2026-01-01T{hours:02}:{minutes:02}:00Z")
}

fn logistic(x: f64) -> f64 {
    1.0 / (1.0 + (-x).exp())
}

fn round_to(v: f64, decimals: u32) -> f64 {
    let f = 10f64.powi(decimals as i32);
    (v * f).round() / f
}

fn build_sessions() -> Vec<SessionData> {
    let mut sessions: Vec<SessionData> = (0..NUM_SESSIONS).map(synth_session).collect();

    let summaries: Vec<SessionSummary> = sessions
        .iter()
        .map(|s| SessionSummary {
            window_start: s.metadata.window_start_utc.clone().unwrap_or_default(),
            initial_btc: s.initial_btc_price_usd.unwrap_or(0.0),
            final_btc: s.final_btc_price_usd.unwrap_or(0.0),
            outcome: s.outcome.clone().unwrap_or_default(),
        })
        .collect();

    pre_slice_summaries(&mut sessions, &summaries, WINDOW_DURATION);
    sessions
}

// ---------------------------------------------------------------------------
// Snapshot
// ---------------------------------------------------------------------------

/// Round hard before comparing. Floating-point association can shift the last
/// couple of ULPs when arithmetic is reordered (e.g. array indexing instead of
/// named fields) without any behavioural change; 6 decimal places on dollar
/// amounts is far tighter than anything that could matter and still immune to
/// that noise.
fn r6(v: f64) -> f64 {
    (v * 1_000_000.0).round() / 1_000_000.0
}

fn snapshot_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("golden")
        .join("backtest_snapshot.json")
}

/// Run every registered strategy over the synthetic sessions and reduce to a
/// comparable summary. Ordered maps so the serialized form is stable.
fn compute_snapshot() -> serde_json::Value {
    let sessions = build_sessions();
    let registry = all_strategies();

    // Deterministic strategy order.
    let mut names: Vec<&String> = registry.keys().collect();
    names.sort();

    let mut out = BTreeMap::new();

    for name in names {
        let factory = registry[name];
        let mut strategy = factory();

        // Mirror the backtest CLI defaults: book execution, fees on, no latency.
        let sim = Simulator::new(100.0, "book", 0.0, true, None, 0, None);
        let result = sim.run_backtest(strategy.as_mut(), &sessions);

        // Per-session PnL is the real behavioural fingerprint — aggregates can
        // coincidentally match while individual sessions diverge.
        let per_session: Vec<serde_json::Value> = result
            .sessions
            .iter()
            .map(|s| {
                serde_json::json!({
                    "pnl": r6(s.pnl),
                    "trades": s.trades.len(),
                    "fees": r6(s.total_fees),
                })
            })
            .collect();

        // Full trade tape for the first few sessions that actually traded, so a
        // regression in side/price/size shows up as a readable diff rather than
        // just a PnL delta.
        let mut sample_tape = Vec::new();
        for s in result.sessions.iter().filter(|s| !s.trades.is_empty()).take(3) {
            let trades: Vec<serde_json::Value> = s
                .trades
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "side": t.side,
                        "price": r6(t.price),
                        "tokens": r6(t.tokens),
                        "amount": r6(t.amount),
                        "elapsed_seconds": t.elapsed_seconds.map(r6),
                    })
                })
                .collect();
            sample_tape.push(serde_json::json!({
                "session_file": s.session_file,
                "outcome": s.outcome,
                "trades": trades,
            }));
        }

        out.insert(
            name.clone(),
            serde_json::json!({
                "version": strategy.version(),
                "total_pnl": r6(result.total_pnl),
                "mean_pnl": r6(result.mean_pnl),
                "entry_rate": r6(result.entry_rate),
                "win_rate": r6(result.win_rate),
                "sharpe": r6(result.sharpe),
                "total_fees": r6(result.total_fees),
                "num_sessions": result.num_sessions,
                "per_session": per_session,
                "sample_tape": sample_tape,
            }),
        );
    }

    serde_json::json!({
        "config": {
            "num_sessions": NUM_SESSIONS,
            "window_duration": WINDOW_DURATION,
            "sample_interval": SAMPLE_INTERVAL,
            "execution_mode": "book",
            "starting_cash": 100.0,
            "fees": true,
        },
        "strategies": out,
    })
}

#[test]
fn golden_backtest_is_unchanged() {
    let current = compute_snapshot();
    let path = snapshot_path();

    if std::env::var("UPDATE_GOLDEN").is_ok() || !path.exists() {
        std::fs::create_dir_all(path.parent().unwrap()).expect("create tests/golden");
        std::fs::write(
            &path,
            serde_json::to_string_pretty(&current).expect("serialize snapshot"),
        )
        .expect("write snapshot");
        if std::env::var("UPDATE_GOLDEN").is_ok() {
            eprintln!("golden snapshot regenerated at {}", path.display());
            return;
        }
        eprintln!(
            "golden snapshot created at {} — commit it, then re-run",
            path.display()
        );
        return;
    }

    let expected: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).expect("read snapshot"))
            .expect("parse snapshot");

    if current == expected {
        return;
    }

    // Narrow the diff to the first offending strategy so the failure is
    // readable instead of dumping two large JSON blobs.
    let cur_strats = current.get("strategies").and_then(|v| v.as_object());
    let exp_strats = expected.get("strategies").and_then(|v| v.as_object());
    if let (Some(cur), Some(exp)) = (cur_strats, exp_strats) {
        for (name, exp_val) in exp {
            match cur.get(name) {
                None => panic!("golden backtest: strategy '{name}' disappeared from the registry"),
                Some(cur_val) if cur_val != exp_val => {
                    panic!(
                        "golden backtest changed for strategy '{name}'.\n\
                         This means the refactor altered trading behaviour.\n\
                         expected: {}\n\ngot: {}",
                        serde_json::to_string_pretty(exp_val).unwrap(),
                        serde_json::to_string_pretty(cur_val).unwrap(),
                    );
                }
                _ => {}
            }
        }
        for name in cur.keys() {
            if !exp.contains_key(name) {
                panic!(
                    "golden backtest: new strategy '{name}' is not in the snapshot. \
                     Re-run with UPDATE_GOLDEN=1 if that is intended."
                );
            }
        }
    }

    panic!("golden backtest snapshot differs (config block changed)");
}

/// The synthesiser itself must be deterministic, otherwise the snapshot proves
/// nothing.
#[test]
fn synthesis_is_deterministic() {
    let a = build_sessions();
    let b = build_sessions();
    assert_eq!(a.len(), b.len());
    for (x, y) in a.iter().zip(b.iter()) {
        assert_eq!(x.outcome, y.outcome);
        assert_eq!(x.samples.len(), y.samples.len());
        assert_eq!(x.initial_btc_price_usd, y.initial_btc_price_usd);
        assert_eq!(x.final_btc_price_usd, y.final_btc_price_usd);
        for (s1, s2) in x.samples.iter().zip(y.samples.iter()) {
            assert_eq!(s1.btc_price_usd, s2.btc_price_usd);
            assert_eq!(s1.up_token_midpoint, s2.up_token_midpoint);
        }
    }
}

/// A snapshot where nothing ever trades would silently pass any refactor.
/// Every registered strategy must fire at least once, and the tape must contain
/// both buy and sell legs on both sides — otherwise whole branches of
/// `compute_trade_size` / `apply_trade` / `Side` routing go unguarded.
#[test]
fn synthetic_sessions_actually_trigger_trades() {
    let sessions = build_sessions();
    let registry = all_strategies();

    let mut silent = Vec::new();
    let mut sides_seen: BTreeMap<String, usize> = BTreeMap::new();
    let mut total_trades = 0usize;

    for (name, factory) in &registry {
        let mut strategy = factory();
        let sim = Simulator::new(100.0, "book", 0.0, true, None, 0, None);
        let result = sim.run_backtest(strategy.as_mut(), &sessions);

        let trades: usize = result.sessions.iter().map(|s| s.trades.len()).sum();
        total_trades += trades;
        if trades == 0 {
            silent.push(name.clone());
        }
        for s in &result.sessions {
            for t in &s.trades {
                *sides_seen.entry(t.side.clone()).or_default() += 1;
            }
        }
    }

    assert!(
        silent.is_empty(),
        "these strategies never traded on the synthetic set, so the golden \
         snapshot does not guard their execution path: {silent:?}"
    );

    for side in ["buy_up", "buy_down", "sell_up", "sell_down"] {
        assert!(
            sides_seen.contains_key(side),
            "no '{side}' trade in the synthetic tape — that branch is unguarded. \
             Observed: {sides_seen:?}"
        );
    }

    assert!(
        total_trades >= 100,
        "only {total_trades} trades across the whole set — too thin to be a \
         meaningful guard"
    );
}

/// Both outcomes must be represented, otherwise `Position::resolve` is only
/// exercised on one branch.
#[test]
fn synthetic_sessions_cover_both_outcomes() {
    let sessions = build_sessions();
    let ups = sessions
        .iter()
        .filter(|s| s.outcome.as_deref() == Some("Up"))
        .count();
    let downs = sessions.len() - ups;
    assert!(ups >= 5, "only {ups} Up sessions");
    assert!(downs >= 5, "only {downs} Down sessions");
}

/// Diagnostic — not part of the guard. Run with:
///     cargo test --test golden_backtest regime_diagnostic -- --ignored --nocapture
#[test]
#[ignore]
fn regime_diagnostic() {
    use fondaco::volatility::classify_regime;
    let sessions = build_sessions();
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for s in &sessions {
        let prior = s._prior_summaries.clone().unwrap_or_default();
        let r = classify_regime(&prior, 20, 10, None, 1.0, Some(0.5), Some(600.0));
        let key = format!("{:?}/{:?}", r.vol, r.trend);
        *counts.entry(key).or_default() += 1;
    }
    for (k, v) in &counts {
        println!("{k:40} {v}");
    }
}
