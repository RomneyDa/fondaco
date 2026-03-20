//! Session-level volatility computations shared across strategies.

use crate::defaults::BPS_DIVISOR;
use crate::models::SessionSummary;

/// Compute mean and std of absolute session returns in bps.
pub fn compute_session_volatility(
    prior_summaries: &[SessionSummary],
    lookback: usize,
    min_sessions: usize,
) -> Option<(f64, f64)> {
    let valid: Vec<&SessionSummary> = prior_summaries
        .iter()
        .filter(|s| s.initial_btc > 0.0 && s.final_btc > 0.0)
        .collect();

    let start = if valid.len() > lookback {
        valid.len() - lookback
    } else {
        0
    };
    let recent = &valid[start..];

    let min_required = min_sessions.max((lookback as f64 * 0.9) as usize);
    if recent.len() < min_required {
        return None;
    }

    let abs_returns_bps: Vec<f64> = recent
        .iter()
        .map(|s| ((s.final_btc - s.initial_btc) / s.initial_btc * BPS_DIVISOR).abs())
        .collect();

    let mean_ret: f64 = abs_returns_bps.iter().sum::<f64>() / abs_returns_bps.len() as f64;
    let vol_std = if abs_returns_bps.len() > 1 {
        let variance: f64 = abs_returns_bps
            .iter()
            .map(|r| (r - mean_ret).powi(2))
            .sum::<f64>()
            / (abs_returns_bps.len() - 1) as f64;
        variance.sqrt()
    } else {
        mean_ret
    };

    Some((mean_ret, vol_std))
}

/// Compute mean and population std of signed session returns in bps.
pub fn compute_signed_volatility(
    prior_summaries: &[SessionSummary],
    lookback: usize,
    min_sessions: usize,
) -> Option<(f64, f64)> {
    let valid: Vec<f64> = prior_summaries
        .iter()
        .filter(|s| s.initial_btc > 0.0)
        .map(|s| (s.final_btc / s.initial_btc - 1.0) * BPS_DIVISOR)
        .collect();

    let start = if valid.len() > lookback {
        valid.len() - lookback
    } else {
        0
    };
    let recent = &valid[start..];

    let min_required = min_sessions.max((lookback as f64 * 0.9) as usize);
    if recent.len() < min_required {
        return None;
    }

    let mean_ret: f64 = recent.iter().sum::<f64>() / recent.len() as f64;
    let variance: f64 = recent.iter().map(|r| (r - mean_ret).powi(2)).sum::<f64>()
        / recent.len() as f64;
    let vol_std = variance.sqrt();

    Some((mean_ret, vol_std))
}

// Volatility regime thresholds (in bps of absolute session return).
pub const VOL_LOW_THRESHOLD_BPS: f64 = 3.0;
pub const VOL_HIGH_THRESHOLD_BPS: f64 = 8.0;
pub const AUTOCORR_TREND_THRESHOLD: f64 = 0.15;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VolRegime {
    Low,
    Medium,
    High,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TrendRegime {
    Trending,
    MeanReverting,
    Flat,
}

pub fn classify_vol_regime(
    prior_summaries: &[SessionSummary],
    lookback: usize,
    min_sessions: usize,
) -> Option<VolRegime> {
    let vol = compute_session_volatility(prior_summaries, lookback, min_sessions)?;
    let (mean_abs, _) = vol;
    if mean_abs < VOL_LOW_THRESHOLD_BPS {
        Some(VolRegime::Low)
    } else if mean_abs >= VOL_HIGH_THRESHOLD_BPS {
        Some(VolRegime::High)
    } else {
        Some(VolRegime::Medium)
    }
}

pub fn classify_trend_regime(
    prior_summaries: &[SessionSummary],
    lookback: usize,
    min_sessions: usize,
) -> Option<TrendRegime> {
    let returns: Vec<f64> = prior_summaries
        .iter()
        .filter(|s| s.initial_btc > 0.0)
        .map(|s| (s.final_btc / s.initial_btc - 1.0) * BPS_DIVISOR)
        .collect();

    let start = if returns.len() > lookback {
        returns.len() - lookback
    } else {
        0
    };
    let recent = &returns[start..];

    if recent.len() < min_sessions {
        return None;
    }

    let mean_r: f64 = recent.iter().sum::<f64>() / recent.len() as f64;
    let n = recent.len();
    let var: f64 = recent.iter().map(|r| (r - mean_r).powi(2)).sum::<f64>() / n as f64;

    if var < 1e-12 {
        return Some(TrendRegime::Flat);
    }

    let cov: f64 = (0..n - 1)
        .map(|i| (recent[i] - mean_r) * (recent[i + 1] - mean_r))
        .sum::<f64>()
        / (n - 1) as f64;
    let autocorr = cov / var;

    if autocorr > AUTOCORR_TREND_THRESHOLD {
        Some(TrendRegime::Trending)
    } else if autocorr < -AUTOCORR_TREND_THRESHOLD {
        Some(TrendRegime::MeanReverting)
    } else {
        Some(TrendRegime::Flat)
    }
}

#[derive(Debug, Clone)]
pub struct RegimeInfo {
    pub vol: Option<VolRegime>,
    pub trend: Option<TrendRegime>,
    pub data_quality: String,
    pub density: f64,
    pub gap_seconds: Option<f64>,
}

pub fn classify_regime(
    prior_summaries: &[SessionSummary],
    lookback: usize,
    min_sessions: usize,
    gap_before_seconds: Option<f64>,
    density: f64,
    min_density: Option<f64>,
    max_gap_s: Option<f64>,
) -> RegimeInfo {
    let vol = classify_vol_regime(prior_summaries, lookback, min_sessions);
    let trend = classify_trend_regime(prior_summaries, lookback, min_sessions);

    let data_quality =
        if let (Some(max_gap), Some(gap)) = (max_gap_s, gap_before_seconds) {
            if gap > max_gap {
                "stale".to_string()
            } else if let Some(md) = min_density {
                if density < md {
                    "stale".to_string()
                } else {
                    "clean".to_string()
                }
            } else {
                "clean".to_string()
            }
        } else if let Some(md) = min_density {
            if density < md {
                "stale".to_string()
            } else {
                "clean".to_string()
            }
        } else {
            "clean".to_string()
        };

    RegimeInfo {
        vol,
        trend,
        data_quality,
        density,
        gap_seconds: gap_before_seconds,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_summaries(returns_bps: &[f64]) -> Vec<SessionSummary> {
        returns_bps
            .iter()
            .enumerate()
            .map(|(i, &r)| {
                let initial = 60000.0;
                let final_btc = initial * (1.0 + r / BPS_DIVISOR);
                SessionSummary {
                    window_start: format!("2026-01-01T{:02}:00:00Z", i),
                    initial_btc: initial,
                    final_btc,
                    outcome: if r > 0.0 {
                        "Up".to_string()
                    } else {
                        "Down".to_string()
                    },
                }
            })
            .collect()
    }

    #[test]
    fn test_session_volatility() {
        let summaries = make_summaries(&[5.0, -3.0, 7.0, -2.0, 4.0]);
        let (mean, _std) = compute_session_volatility(&summaries, 5, 3).unwrap();
        // Mean absolute return
        let expected = (5.0 + 3.0 + 7.0 + 2.0 + 4.0) / 5.0;
        assert!((mean - expected).abs() < 0.01);
    }

    #[test]
    fn test_vol_regime_low() {
        let summaries = make_summaries(&[1.0, -1.0, 0.5, -0.5, 1.0]);
        let regime = classify_vol_regime(&summaries, 5, 3);
        assert_eq!(regime, Some(VolRegime::Low));
    }

    #[test]
    fn test_vol_regime_high() {
        let summaries = make_summaries(&[10.0, -12.0, 15.0, -8.0, 11.0]);
        let regime = classify_vol_regime(&summaries, 5, 3);
        assert_eq!(regime, Some(VolRegime::High));
    }

    #[test]
    fn test_signed_volatility() {
        let summaries = make_summaries(&[5.0, -3.0, 7.0, -2.0, 4.0]);
        let result = compute_signed_volatility(&summaries, 5, 3);
        assert!(result.is_some());
    }
}
