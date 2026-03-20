//! Centralized session statistics cache with bisect-based lookups.

use crate::defaults::BPS_DIVISOR;
use crate::models::SessionSummary;
use chrono::{DateTime, FixedOffset};

#[derive(Debug, Clone)]
pub struct SessionStats {
    pub window_start: String,
    pub outcome: String,
    pub initial_btc: f64,
    pub final_btc: f64,
    pub return_bps: f64,
    pub abs_return_bps: f64,
    pub source_file: String,
}

impl SessionStats {
    pub fn to_summary(&self) -> SessionSummary {
        SessionSummary {
            window_start: self.window_start.clone(),
            initial_btc: self.initial_btc,
            final_btc: self.final_btc,
            outcome: self.outcome.clone(),
        }
    }
}

fn make_stats(
    window_start: &str,
    outcome: &str,
    initial_btc: f64,
    final_btc: f64,
    source_file: &str,
) -> Option<SessionStats> {
    if initial_btc <= 0.0 || final_btc <= 0.0 || outcome.is_empty() {
        return None;
    }
    let ret = (final_btc - initial_btc) / initial_btc * BPS_DIVISOR;
    Some(SessionStats {
        window_start: window_start.to_string(),
        outcome: outcome.to_string(),
        initial_btc,
        final_btc,
        return_bps: ret,
        abs_return_bps: ret.abs(),
        source_file: source_file.to_string(),
    })
}

pub struct SessionStatsCache {
    entries: Vec<SessionStats>,
    starts: Vec<String>,
}

impl SessionStatsCache {
    pub fn new() -> Self {
        Self {
            entries: Vec::new(),
            starts: Vec::new(),
        }
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn from_summaries(summaries: &[SessionSummary]) -> Self {
        let mut cache = Self::new();
        for s in summaries {
            if let Some(stats) = make_stats(
                &s.window_start,
                &s.outcome,
                s.initial_btc,
                s.final_btc,
                "",
            ) {
                cache.entries.push(stats);
            }
        }
        cache.entries.sort_by(|a, b| a.window_start.cmp(&b.window_start));
        cache.starts = cache.entries.iter().map(|e| e.window_start.clone()).collect();
        cache
    }

    /// Return the last N sessions strictly before `before` timestamp.
    pub fn prior(&self, before: &str, n: usize) -> Vec<SessionSummary> {
        let idx = self.starts.partition_point(|s| s.as_str() < before);
        let start = idx.saturating_sub(n);
        self.entries[start..idx]
            .iter()
            .map(|e| e.to_summary())
            .collect()
    }

    /// Seconds between the session at `window_start` and the one before it.
    pub fn gap_before(&self, window_start: &str) -> Option<f64> {
        let idx = self.starts.partition_point(|s| s.as_str() < window_start);
        if idx == 0 {
            return None;
        }
        let current = parse_ts(window_start).ok()?;
        let prev = parse_ts(&self.starts[idx - 1]).ok()?;
        Some((current - prev).num_seconds() as f64)
    }

    /// Fraction of expected sessions present in the lookback window.
    pub fn data_density(&self, before: &str, n: usize, expected_interval_s: Option<f64>) -> f64 {
        let entries = self.prior(before, n);
        if entries.len() < 2 {
            return 1.0;
        }
        let expected_interval_s = match expected_interval_s {
            Some(v) => v,
            None => return 1.0,
        };
        let first_ts = match parse_ts(&entries[0].window_start) {
            Ok(v) => v,
            Err(_) => return 1.0,
        };
        let last_ts = match parse_ts(&entries[entries.len() - 1].window_start) {
            Ok(v) => v,
            Err(_) => return 1.0,
        };
        let span_s = (last_ts - first_ts).num_seconds() as f64;
        if span_s <= 0.0 {
            return 1.0;
        }
        let expected_count = span_s / expected_interval_s + 1.0;
        if expected_count <= 0.0 {
            return 1.0;
        }
        (entries.len() as f64 / expected_count).min(1.0)
    }
}

fn parse_ts(s: &str) -> Result<DateTime<FixedOffset>, chrono::ParseError> {
    let s = if s.ends_with('Z') {
        format!("{}+00:00", &s[..s.len() - 1])
    } else {
        s.to_string()
    };
    DateTime::parse_from_rfc3339(&s).or_else(|_| {
        // Try ISO without timezone
        DateTime::parse_from_str(&format!("{}+00:00", s), "%Y-%m-%dT%H:%M:%S%.f%:z")
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_test_summaries() -> Vec<SessionSummary> {
        (0..10)
            .map(|i| SessionSummary {
                window_start: format!("2026-01-01T{:02}:00:00Z", i),
                initial_btc: 60000.0,
                final_btc: 60000.0 + (i as f64 * 10.0),
                outcome: if i % 2 == 0 {
                    "Up".to_string()
                } else {
                    "Down".to_string()
                },
            })
            .collect()
    }

    #[test]
    fn test_prior() {
        let summaries = make_test_summaries();
        let cache = SessionStatsCache::from_summaries(&summaries);
        let prior = cache.prior("2026-01-01T05:00:00Z", 3);
        assert_eq!(prior.len(), 3);
    }

    #[test]
    fn test_gap_before() {
        let summaries = make_test_summaries();
        let cache = SessionStatsCache::from_summaries(&summaries);
        let gap = cache.gap_before("2026-01-01T01:00:00Z");
        assert!(gap.is_some());
        assert!((gap.unwrap() - 3600.0).abs() < 1.0);
    }

    #[test]
    fn test_data_density() {
        let summaries = make_test_summaries();
        let cache = SessionStatsCache::from_summaries(&summaries);
        let density = cache.data_density("2026-01-01T09:00:00Z", 5, Some(3600.0));
        assert!(density > 0.0 && density <= 1.0);
    }
}
