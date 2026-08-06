//! Feed-latency measurement.
//!
//! This is Phase 1's real deliverable. Everything downstream depends on one
//! question: **when a score changes, does the book move before or after the
//! public feeds report it?**
//!
//! If the book consistently moves first, there is no event-reaction edge and
//! the whole strategy has to come from model disagreement instead — a much
//! harder business. That is worth knowing before writing a single strategy,
//! and it is cheap to measure, so it is measured from day one.
//!
//! For each material game event we record:
//!   - when the Polymarket sports feed reported it
//!   - when ESPN reported it (if at all)
//!   - when the book first moved beyond a threshold afterwards
//!
//! and emit one JSONL record per event to `data/markets/<league>/latency.jsonl`.

use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::time::Instant;

use serde::{Deserialize, Serialize};

use super::models::GameState;

/// Minimum midpoint move counted as "the book reacted".
pub const BOOK_MOVE_THRESHOLD: f64 = 0.01;
/// How long after an event we keep waiting for the book to move.
pub const OBSERVATION_WINDOW_S: f64 = 60.0;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LatencyRecord {
    pub game_id: i64,
    pub league_key: String,
    pub event_utc: String,
    /// What changed: `score`, `red_card`, `period`, `review`.
    pub event_kind: String,
    pub home_score: i32,
    pub away_score: i32,
    /// Seconds from the sports-feed report to the first book move.
    /// Negative means the book moved *first* — no reaction edge.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub feed_to_book_s: Option<f64>,
    /// Seconds from the sports feed to the ESPN report of the same event.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub feed_to_espn_s: Option<f64>,
    /// Midpoint before and after, for the leg that moved most.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub midpoint_before: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub midpoint_after: Option<f64>,
    /// True if the observation window expired without a qualifying book move.
    pub book_never_moved: bool,
}

/// An event awaiting a book reaction.
struct PendingEvent {
    game_id: i64,
    event_utc: String,
    event_kind: String,
    home_score: i32,
    away_score: i32,
    observed_at: Instant,
    baseline_midpoints: Vec<Option<f64>>,
    espn_seen_at: Option<Instant>,
}

/// Tracks events and the book's response to them.
pub struct LatencyTracker {
    league_key: String,
    path: PathBuf,
    pending: Vec<PendingEvent>,
    last_state: HashMap<i64, GameState>,
    records_written: usize,
}

impl LatencyTracker {
    pub fn new(data_dir: &str, league_key: &str) -> Self {
        let path = PathBuf::from(data_dir)
            .join("markets")
            .join(league_key)
            .join("latency.jsonl");
        Self {
            league_key: league_key.to_string(),
            path,
            pending: Vec::new(),
            last_state: HashMap::new(),
            records_written: 0,
        }
    }

    pub fn records_written(&self) -> usize {
        self.records_written
    }

    pub fn pending_count(&self) -> usize {
        self.pending.len()
    }

    /// Classify what changed between two states.
    fn classify(prev: &GameState, next: &GameState) -> Option<&'static str> {
        if prev.home_score != next.home_score || prev.away_score != next.away_score {
            Some("score")
        } else if prev.home_red_cards != next.home_red_cards
            || prev.away_red_cards != next.away_red_cards
        {
            Some("red_card")
        } else if prev.under_review != next.under_review {
            Some("review")
        } else if prev.status != next.status {
            Some("period")
        } else {
            None
        }
    }

    /// Record a game-state observation. Starts a pending measurement when the
    /// state materially changed.
    pub fn observe_state(
        &mut self,
        now: Instant,
        game_id: i64,
        state: &GameState,
        current_midpoints: &[Option<f64>],
        event_utc: &str,
    ) {
        let kind = match self.last_state.get(&game_id) {
            Some(prev) => Self::classify(prev, state),
            None => None,
        };
        self.last_state.insert(game_id, state.clone());

        if let Some(kind) = kind {
            self.pending.push(PendingEvent {
                game_id,
                event_utc: event_utc.to_string(),
                event_kind: kind.to_string(),
                home_score: state.home_score,
                away_score: state.away_score,
                observed_at: now,
                baseline_midpoints: current_midpoints.to_vec(),
                espn_seen_at: None,
            });
        }
    }

    /// Note that ESPN has reported the same scoreline, so the two feeds can be
    /// compared.
    pub fn observe_espn(&mut self, now: Instant, game_id: i64, home: i32, away: i32) {
        for p in self.pending.iter_mut() {
            if p.game_id == game_id
                && p.home_score == home
                && p.away_score == away
                && p.espn_seen_at.is_none()
            {
                p.espn_seen_at = Some(now);
            }
        }
    }

    /// Record the current book and resolve any pending event whose book has
    /// now moved (or whose window has expired).
    pub fn observe_book(&mut self, now: Instant, game_id: i64, midpoints: &[Option<f64>]) {
        let mut finished = Vec::new();

        for (i, p) in self.pending.iter().enumerate() {
            if p.game_id != game_id {
                continue;
            }

            let moved = largest_move(&p.baseline_midpoints, midpoints);
            let elapsed = now.duration_since(p.observed_at).as_secs_f64();

            if let Some((before, after, delta)) = moved {
                if delta >= BOOK_MOVE_THRESHOLD {
                    finished.push((i, Some((before, after)), elapsed, false));
                    continue;
                }
            }
            if elapsed >= OBSERVATION_WINDOW_S {
                finished.push((i, None, elapsed, true));
            }
        }

        // Remove from the back so indices stay valid.
        for (idx, moved, elapsed, never) in finished.into_iter().rev() {
            let p = self.pending.remove(idx);
            let record = LatencyRecord {
                game_id: p.game_id,
                league_key: self.league_key.clone(),
                event_utc: p.event_utc,
                event_kind: p.event_kind,
                home_score: p.home_score,
                away_score: p.away_score,
                feed_to_book_s: if never { None } else { Some(elapsed) },
                feed_to_espn_s: p
                    .espn_seen_at
                    .map(|t| t.duration_since(p.observed_at).as_secs_f64()),
                midpoint_before: moved.map(|(b, _)| b),
                midpoint_after: moved.map(|(_, a)| a),
                book_never_moved: never,
            };
            self.write(&record);
        }
    }

    /// Expire pending events for games that have gone quiet, so the tracker
    /// does not grow without bound over a long slate.
    pub fn expire_stale(&mut self, now: Instant) {
        let mut expired = Vec::new();
        for (i, p) in self.pending.iter().enumerate() {
            if now.duration_since(p.observed_at).as_secs_f64() >= OBSERVATION_WINDOW_S {
                expired.push(i);
            }
        }
        for idx in expired.into_iter().rev() {
            let p = self.pending.remove(idx);
            let record = LatencyRecord {
                game_id: p.game_id,
                league_key: self.league_key.clone(),
                event_utc: p.event_utc,
                event_kind: p.event_kind,
                home_score: p.home_score,
                away_score: p.away_score,
                feed_to_book_s: None,
                feed_to_espn_s: p
                    .espn_seen_at
                    .map(|t| t.duration_since(p.observed_at).as_secs_f64()),
                midpoint_before: None,
                midpoint_after: None,
                book_never_moved: true,
            };
            self.write(&record);
        }
    }

    fn write(&mut self, record: &LatencyRecord) {
        if let Some(parent) = self.path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(line) = serde_json::to_string(record) {
            if let Ok(mut f) = fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&self.path)
            {
                let _ = writeln!(f, "{line}");
                self.records_written += 1;
            }
        }
    }
}

/// Largest absolute midpoint move across legs, as `(before, after, delta)`.
fn largest_move(
    baseline: &[Option<f64>],
    current: &[Option<f64>],
) -> Option<(f64, f64, f64)> {
    let mut best: Option<(f64, f64, f64)> = None;
    for (b, c) in baseline.iter().zip(current.iter()) {
        if let (Some(b), Some(c)) = (b, c) {
            let delta = (c - b).abs();
            if best.map(|(_, _, d)| delta > d).unwrap_or(true) {
                best = Some((*b, *c, delta));
            }
        }
    }
    best
}

/// Summary over a collected latency file, for the report.
#[derive(Debug, Clone, Default, Serialize)]
pub struct LatencySummary {
    pub events: usize,
    pub with_book_move: usize,
    pub never_moved: usize,
    /// Median seconds from feed report to book move.
    pub median_feed_to_book_s: Option<f64>,
    /// Fraction of events where the book had already moved before the feed
    /// reported. This is the number that decides whether reaction edge exists.
    pub fraction_book_first: f64,
    pub median_feed_to_espn_s: Option<f64>,
}

pub fn summarize(records: &[LatencyRecord]) -> LatencySummary {
    let mut summary = LatencySummary {
        events: records.len(),
        ..Default::default()
    };
    if records.is_empty() {
        return summary;
    }

    let mut book_deltas: Vec<f64> = Vec::new();
    let mut espn_deltas: Vec<f64> = Vec::new();
    let mut book_first = 0usize;

    for r in records {
        if r.book_never_moved {
            summary.never_moved += 1;
        }
        if let Some(d) = r.feed_to_book_s {
            summary.with_book_move += 1;
            book_deltas.push(d);
            if d <= 0.0 {
                book_first += 1;
            }
        }
        if let Some(d) = r.feed_to_espn_s {
            espn_deltas.push(d);
        }
    }

    summary.median_feed_to_book_s = median(&mut book_deltas);
    summary.median_feed_to_espn_s = median(&mut espn_deltas);
    summary.fraction_book_first = if summary.with_book_move > 0 {
        book_first as f64 / summary.with_book_move as f64
    } else {
        0.0
    };
    summary
}

fn median(v: &mut Vec<f64>) -> Option<f64> {
    if v.is_empty() {
        return None;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = v.len();
    Some(if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    })
}

/// Load records written by a collector.
pub fn load_records(data_dir: &str, league_key: &str) -> Vec<LatencyRecord> {
    let path = PathBuf::from(data_dir)
        .join("markets")
        .join(league_key)
        .join("latency.jsonl");
    let content = match fs::read_to_string(path) {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };
    content
        .lines()
        .filter(|l| !l.trim().is_empty())
        .filter_map(|l| serde_json::from_str::<LatencyRecord>(l).ok())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::models::GameStatus;
    use std::time::Duration;
    use tempfile::tempdir;

    fn state(home: i32, away: i32) -> GameState {
        GameState {
            home_score: home,
            away_score: away,
            period: "Q2".into(),
            clock: "07:00".into(),
            status: GameStatus::InProgress,
            elapsed_minutes: Some(23.0),
            home_red_cards: 0,
            away_red_cards: 0,
            under_review: false,
        }
    }

    #[test]
    fn a_score_change_starts_a_pending_measurement() {
        let tmp = tempdir().unwrap();
        let mut t = LatencyTracker::new(tmp.path().to_str().unwrap(), "nfl");
        let now = Instant::now();

        // First observation establishes a baseline; no event yet.
        t.observe_state(now, 1, &state(0, 0), &[Some(0.5), Some(0.5)], "t0");
        assert_eq!(t.pending_count(), 0);

        t.observe_state(now, 1, &state(7, 0), &[Some(0.5), Some(0.5)], "t1");
        assert_eq!(t.pending_count(), 1);
    }

    #[test]
    fn a_clock_tick_does_not_start_a_measurement() {
        let tmp = tempdir().unwrap();
        let mut t = LatencyTracker::new(tmp.path().to_str().unwrap(), "nfl");
        let now = Instant::now();
        t.observe_state(now, 1, &state(0, 0), &[Some(0.5)], "t0");

        let mut later = state(0, 0);
        later.clock = "06:00".into();
        t.observe_state(now, 1, &later, &[Some(0.5)], "t1");
        assert_eq!(t.pending_count(), 0);
    }

    #[test]
    fn resolves_when_the_book_moves_and_records_the_delay() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();
        let mut t = LatencyTracker::new(d, "nfl");
        let t0 = Instant::now();

        t.observe_state(t0, 1, &state(0, 0), &[Some(0.50), Some(0.50)], "t0");
        t.observe_state(t0, 1, &state(7, 0), &[Some(0.50), Some(0.50)], "t1");

        // Book has not moved enough yet.
        t.observe_book(t0 + Duration::from_secs_f64(1.0), 1, &[Some(0.505), Some(0.495)]);
        assert_eq!(t.pending_count(), 1);

        // Now it moves past the threshold.
        t.observe_book(t0 + Duration::from_secs_f64(2.5), 1, &[Some(0.62), Some(0.38)]);
        assert_eq!(t.pending_count(), 0);

        let records = load_records(d, "nfl");
        assert_eq!(records.len(), 1);
        let r = &records[0];
        assert_eq!(r.event_kind, "score");
        assert!(!r.book_never_moved);
        let delay = r.feed_to_book_s.unwrap();
        assert!((delay - 2.5).abs() < 0.1, "got {delay}");
        assert_eq!(r.midpoint_before, Some(0.50));
        assert_eq!(r.midpoint_after, Some(0.62));
    }

    #[test]
    fn an_event_the_book_ignores_is_recorded_as_never_moved() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();
        let mut t = LatencyTracker::new(d, "nfl");
        let t0 = Instant::now();

        t.observe_state(t0, 1, &state(0, 0), &[Some(0.5)], "t0");
        t.observe_state(t0, 1, &state(0, 3), &[Some(0.5)], "t1");

        t.expire_stale(t0 + Duration::from_secs_f64(OBSERVATION_WINDOW_S + 1.0));
        assert_eq!(t.pending_count(), 0);

        let records = load_records(d, "nfl");
        assert_eq!(records.len(), 1);
        assert!(records[0].book_never_moved);
        assert_eq!(records[0].feed_to_book_s, None);
    }

    #[test]
    fn espn_delay_is_captured_when_it_confirms_the_same_score() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();
        let mut t = LatencyTracker::new(d, "nfl");
        let t0 = Instant::now();

        t.observe_state(t0, 1, &state(0, 0), &[Some(0.5)], "t0");
        t.observe_state(t0, 1, &state(7, 0), &[Some(0.5)], "t1");
        t.observe_espn(t0 + Duration::from_secs_f64(4.0), 1, 7, 0);
        t.observe_book(t0 + Duration::from_secs_f64(6.0), 1, &[Some(0.65)]);

        let records = load_records(d, "nfl");
        let espn = records[0].feed_to_espn_s.unwrap();
        assert!((espn - 4.0).abs() < 0.1, "got {espn}");
    }

    #[test]
    fn espn_confirmation_of_a_different_score_is_not_matched() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();
        let mut t = LatencyTracker::new(d, "nfl");
        let t0 = Instant::now();

        t.observe_state(t0, 1, &state(0, 0), &[Some(0.5)], "t0");
        t.observe_state(t0, 1, &state(7, 0), &[Some(0.5)], "t1");
        // ESPN still reporting the old score.
        t.observe_espn(t0 + Duration::from_secs_f64(1.0), 1, 0, 0);
        t.observe_book(t0 + Duration::from_secs_f64(2.0), 1, &[Some(0.65)]);

        let records = load_records(d, "nfl");
        assert_eq!(records[0].feed_to_espn_s, None);
    }

    #[test]
    fn events_for_other_games_do_not_resolve_each_other() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();
        let mut t = LatencyTracker::new(d, "nfl");
        let t0 = Instant::now();

        t.observe_state(t0, 1, &state(0, 0), &[Some(0.5)], "t0");
        t.observe_state(t0, 1, &state(7, 0), &[Some(0.5)], "t1");

        // A different game's book moving must not resolve game 1's event.
        t.observe_book(t0 + Duration::from_secs_f64(1.0), 2, &[Some(0.9)]);
        assert_eq!(t.pending_count(), 1);
    }

    #[test]
    fn summary_reports_the_fraction_where_the_book_led() {
        let mk = |feed_to_book: Option<f64>| LatencyRecord {
            game_id: 1,
            league_key: "nfl".into(),
            event_utc: String::new(),
            event_kind: "score".into(),
            home_score: 0,
            away_score: 0,
            feed_to_book_s: feed_to_book,
            feed_to_espn_s: None,
            midpoint_before: None,
            midpoint_after: None,
            book_never_moved: feed_to_book.is_none(),
        };

        // Two events where the book led (<= 0), two where it followed.
        let records = vec![mk(Some(-1.0)), mk(Some(-0.5)), mk(Some(2.0)), mk(Some(4.0))];
        let s = summarize(&records);
        assert_eq!(s.events, 4);
        assert_eq!(s.with_book_move, 4);
        assert!((s.fraction_book_first - 0.5).abs() < 1e-9);
        assert_eq!(s.median_feed_to_book_s, Some(0.75));
    }

    #[test]
    fn summary_of_an_empty_set_is_harmless() {
        let s = summarize(&[]);
        assert_eq!(s.events, 0);
        assert_eq!(s.median_feed_to_book_s, None);
        assert_eq!(s.fraction_book_first, 0.0);
    }
}
