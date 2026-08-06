//! Kickoff scheduling for sports collectors.
//!
//! The crypto orchestrator spawns long-lived processes because a 5-minute
//! market never stops. Sports is different: a league has games on some days and
//! not others, and running a collector through a 40-hour gap between fixtures
//! is pure waste — and worse, it keeps a WebSocket and a discovery loop alive
//! for nothing.
//!
//! This computes when a league's collector should be running: from
//! `lead_minutes` before the earliest upcoming kickoff until `trail_minutes`
//! after the latest expected finish.

use chrono::{DateTime, Duration as ChronoDuration, Utc};
use serde::Serialize;

use super::models::{MarketGroup, Sport};

/// How long before kickoff to start collecting, and how long after the last
/// expected finish to keep going.
#[derive(Debug, Clone)]
pub struct ScheduleConfig {
    pub lead_minutes: i64,
    pub trail_minutes: i64,
    /// Wall-clock length of a game including stoppages and breaks, used to
    /// estimate when the last game finishes.
    pub expected_duration_minutes: i64,
}

impl ScheduleConfig {
    pub fn for_sport(sport: Sport) -> Self {
        match sport {
            // A soccer match is ~2h wall clock including half time.
            Sport::Soccer => Self {
                lead_minutes: 30,
                trail_minutes: 30,
                expected_duration_minutes: 135,
            },
            // An NFL game routinely runs 3h15m+.
            Sport::AmericanFootball => Self {
                lead_minutes: 30,
                trail_minutes: 45,
                expected_duration_minutes: 220,
            },
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Window {
    pub start_utc: String,
    pub end_utc: String,
    pub games: usize,
}

/// Whether a collector should be running now, and the next window if not.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ScheduleDecision {
    pub should_run: bool,
    /// Seconds until the next window opens. `None` when already running or when
    /// nothing is scheduled.
    pub seconds_until_next: Option<i64>,
    pub current_window: Option<Window>,
    pub next_window: Option<Window>,
}

fn parse(ts: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(ts)
        .ok()
        .map(|d| d.with_timezone(&Utc))
}

/// Merge kickoffs into contiguous coverage windows.
///
/// Games whose windows overlap or touch are merged, so a Sunday slate produces
/// one window rather than thirteen.
pub fn windows(groups: &[MarketGroup], cfg: &ScheduleConfig) -> Vec<Window> {
    let mut spans: Vec<(DateTime<Utc>, DateTime<Utc>)> = groups
        .iter()
        .filter_map(|g| parse(&g.start_time_utc))
        .map(|start| {
            (
                start - ChronoDuration::minutes(cfg.lead_minutes),
                start
                    + ChronoDuration::minutes(cfg.expected_duration_minutes)
                    + ChronoDuration::minutes(cfg.trail_minutes),
            )
        })
        .collect();

    if spans.is_empty() {
        return Vec::new();
    }

    spans.sort_by_key(|(s, _)| *s);

    let mut merged: Vec<(DateTime<Utc>, DateTime<Utc>, usize)> = Vec::new();
    for (s, e) in spans {
        match merged.last_mut() {
            Some((_, prev_end, count)) if s <= *prev_end => {
                if e > *prev_end {
                    *prev_end = e;
                }
                *count += 1;
            }
            _ => merged.push((s, e, 1)),
        }
    }

    merged
        .into_iter()
        .map(|(s, e, n)| Window {
            start_utc: s.to_rfc3339(),
            end_utc: e.to_rfc3339(),
            games: n,
        })
        .collect()
}

/// Decide whether the collector should be running at `now`.
pub fn decide(groups: &[MarketGroup], cfg: &ScheduleConfig, now: DateTime<Utc>) -> ScheduleDecision {
    let ws = windows(groups, cfg);

    let mut current = None;
    let mut next = None;

    for w in &ws {
        let (s, e) = match (parse(&w.start_utc), parse(&w.end_utc)) {
            (Some(s), Some(e)) => (s, e),
            _ => continue,
        };
        if now >= s && now <= e {
            current = Some(w.clone());
            break;
        }
        if now < s && next.is_none() {
            next = Some(w.clone());
        }
    }

    let seconds_until_next = if current.is_some() {
        None
    } else {
        next.as_ref()
            .and_then(|w| parse(&w.start_utc))
            .map(|s| (s - now).num_seconds().max(0))
    };

    ScheduleDecision {
        should_run: current.is_some(),
        seconds_until_next,
        current_window: current,
        next_window: next,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::models::{SportsLeg, SportsMarketKind};

    fn group(start: &str) -> MarketGroup {
        MarketGroup {
            game_id: Some(1),
            league_key: "nfl".into(),
            event_id: "e".into(),
            event_slug: "s".into(),
            event_title: "t".into(),
            home_team: "H".into(),
            away_team: "A".into(),
            start_time_utc: start.into(),
            legs: vec![SportsLeg {
                key: "home".into(),
                label: "H".into(),
                token_id: "t".into(),
                condition_id: "c".into(),
                kind: SportsMarketKind::Moneyline,
                line: None,
            }],
        }
    }

    fn at(ts: &str) -> DateTime<Utc> {
        parse(ts).unwrap()
    }

    fn nfl() -> ScheduleConfig {
        ScheduleConfig::for_sport(Sport::AmericanFootball)
    }

    #[test]
    fn a_single_game_produces_one_window_with_lead_and_trail() {
        let ws = windows(&[group("2026-02-09T18:00:00Z")], &nfl());
        assert_eq!(ws.len(), 1);
        // 30 min lead.
        assert_eq!(at(&ws[0].start_utc), at("2026-02-09T17:30:00Z"));
        // 18:00 + 220 min duration = 21:40, + 45 min trail = 22:25.
        assert_eq!(at(&ws[0].end_utc), at("2026-02-09T22:25:00Z"));
    }

    #[test]
    fn a_sunday_slate_merges_into_one_window() {
        // Thirteen 1pm ET games plus a 4pm and an 8pm.
        let mut groups: Vec<MarketGroup> = (0..13)
            .map(|_| group("2026-02-09T18:00:00Z"))
            .collect();
        groups.push(group("2026-02-09T21:05:00Z"));
        groups.push(group("2026-02-10T01:20:00Z"));

        let ws = windows(&groups, &nfl());
        assert_eq!(
            ws.len(),
            1,
            "an overlapping slate must be one window, not fifteen: {ws:?}"
        );
        assert_eq!(ws[0].games, 15);
    }

    #[test]
    fn games_on_different_days_produce_separate_windows() {
        let ws = windows(
            &[
                group("2026-02-09T18:00:00Z"),
                group("2026-02-16T18:00:00Z"),
            ],
            &nfl(),
        );
        assert_eq!(ws.len(), 2);
    }

    #[test]
    fn should_run_inside_a_window() {
        let groups = vec![group("2026-02-09T18:00:00Z")];
        let d = decide(&groups, &nfl(), at("2026-02-09T19:00:00Z"));
        assert!(d.should_run);
        assert!(d.current_window.is_some());
        assert_eq!(d.seconds_until_next, None);
    }

    #[test]
    fn should_run_during_the_lead_in() {
        let groups = vec![group("2026-02-09T18:00:00Z")];
        let d = decide(&groups, &nfl(), at("2026-02-09T17:45:00Z"));
        assert!(d.should_run, "collection must start before kickoff");
    }

    #[test]
    fn should_not_run_long_before_kickoff_and_reports_the_wait() {
        let groups = vec![group("2026-02-09T18:00:00Z")];
        let d = decide(&groups, &nfl(), at("2026-02-09T12:00:00Z"));
        assert!(!d.should_run);
        // 5h30m until the window opens at 17:30.
        assert_eq!(d.seconds_until_next, Some(5 * 3600 + 30 * 60));
        assert!(d.next_window.is_some());
    }

    #[test]
    fn should_not_run_after_the_trail_has_elapsed() {
        let groups = vec![group("2026-02-09T18:00:00Z")];
        let d = decide(&groups, &nfl(), at("2026-02-09T23:00:00Z"));
        assert!(
            !d.should_run,
            "a 40-hour gap between fixtures should not keep the collector alive"
        );
        assert_eq!(d.next_window, None);
    }

    #[test]
    fn an_empty_schedule_never_runs() {
        let d = decide(&[], &nfl(), at("2026-02-09T18:00:00Z"));
        assert!(!d.should_run);
        assert_eq!(d.seconds_until_next, None);
        assert_eq!(d.next_window, None);
    }

    #[test]
    fn unparseable_start_times_are_skipped_not_defaulted() {
        let ws = windows(&[group("not a timestamp")], &nfl());
        assert!(ws.is_empty());
    }

    #[test]
    fn soccer_uses_a_shorter_expected_duration_than_football() {
        let soccer = ScheduleConfig::for_sport(Sport::Soccer);
        let football = ScheduleConfig::for_sport(Sport::AmericanFootball);
        assert!(soccer.expected_duration_minutes < football.expected_duration_minutes);
    }

    #[test]
    fn the_next_window_is_the_soonest_future_one() {
        let groups = vec![
            group("2026-02-16T18:00:00Z"),
            group("2026-02-09T18:00:00Z"),
        ];
        let d = decide(&groups, &nfl(), at("2026-02-08T00:00:00Z"));
        assert_eq!(
            at(&d.next_window.unwrap().start_utc),
            at("2026-02-09T17:30:00Z"),
            "must pick the soonest window even when input order is reversed"
        );
    }
}
