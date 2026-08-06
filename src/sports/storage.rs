//! Per-game append-only storage.
//!
//! `session_io::write_live_session` re-serialises the *entire* session every
//! two seconds. For a 5-minute crypto window that is fine. For a 3.5 hour NFL
//! game it is quadratic: a growing multi-megabyte blob rewritten thousands of
//! times, times up to 15 concurrent games.
//!
//! Sports storage is therefore append-only JSONL for the sample stream, with a
//! small separately-written header for live status. Layout:
//!
//! ```text
//! data/markets/nfl/games/2026-02-09--eagles-chiefs--5127839/
//!   meta.json        market group, legs, token ids, teams, kickoff
//!   samples.jsonl    append-only, one line per sample
//!   state.json       small live header, cheap to rewrite often
//!   model.jsonl      fair-value trace
//!   trades.jsonl
//!   decisions.jsonl  every considered trade and the gate that blocked it
//! ```

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use chrono::Utc;
use log::warn;
use serde::Serialize;

use super::models::{GameSample, GameSessionMeta, MarketGroup};

pub fn league_dir(data_dir: &str, league_key: &str) -> PathBuf {
    PathBuf::from(data_dir)
        .join("markets")
        .join(league_key)
        .join("games")
}

pub fn game_dir(data_dir: &str, league_key: &str, group: &MarketGroup) -> PathBuf {
    league_dir(data_dir, league_key).join(group.dir_name())
}

/// Handle to one game's on-disk data. Keeps the samples file open so appends
/// do not pay an open/close per sample.
pub struct GameStore {
    dir: PathBuf,
    samples: Option<fs::File>,
    sample_count: usize,
}

impl GameStore {
    pub fn create(data_dir: &str, league_key: &str, meta: &GameSessionMeta) -> std::io::Result<Self> {
        let dir = game_dir(data_dir, league_key, &meta.group);
        fs::create_dir_all(&dir)?;

        let store = Self {
            dir,
            samples: None,
            sample_count: 0,
        };
        store.write_meta(meta)?;
        Ok(store)
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    pub fn sample_count(&self) -> usize {
        self.sample_count
    }

    pub fn write_meta(&self, meta: &GameSessionMeta) -> std::io::Result<()> {
        write_json_atomic(&self.dir.join("meta.json"), meta)
    }

    /// Append one sample. Flushed each time so a crash loses at most the
    /// in-flight line — a 3-hour game is too long to risk buffering.
    pub fn append_sample(&mut self, sample: &GameSample) -> std::io::Result<()> {
        let line = serde_json::to_string(sample)?;
        if self.samples.is_none() {
            let path = self.dir.join("samples.jsonl");
            self.samples = Some(fs::OpenOptions::new().create(true).append(true).open(path)?);
        }
        let f = self.samples.as_mut().unwrap();
        writeln!(f, "{line}")?;
        f.flush()?;
        self.sample_count += 1;
        Ok(())
    }

    pub fn append_model(&self, entry: &impl Serialize) -> std::io::Result<()> {
        self.append_raw("model.jsonl", entry)
    }

    pub fn append_trade(&self, entry: &impl Serialize) -> std::io::Result<()> {
        self.append_raw("trades.jsonl", entry)
    }

    /// Append a decision record.
    ///
    /// Sports makes few decisions per game, so unlike crypto at 18 Hz we can
    /// afford to log *every* tick where a strategy considered trading, along
    /// with the gate that blocked it. This is the single most useful debugging
    /// artefact the sports path produces.
    pub fn append_decision(&self, entry: &impl Serialize) -> std::io::Result<()> {
        self.append_raw("decisions.jsonl", entry)
    }

    fn append_raw(&self, name: &str, entry: &impl Serialize) -> std::io::Result<()> {
        let line = serde_json::to_string(entry)?;
        let path = self.dir.join(name);
        let mut f = fs::OpenOptions::new().create(true).append(true).open(path)?;
        writeln!(f, "{line}")?;
        Ok(())
    }

    /// Small live header. Safe to rewrite frequently because it is O(1) in the
    /// length of the game.
    pub fn write_state(&self, state: &impl Serialize) -> std::io::Result<()> {
        write_json_atomic(&self.dir.join("state.json"), state)
    }

    pub fn finalize(&self, meta: &GameSessionMeta) -> std::io::Result<()> {
        self.write_meta(meta)
    }
}

fn write_json_atomic(path: &Path, value: &impl Serialize) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(serde_json::to_string_pretty(value)?.as_bytes())?;
        f.flush()?;
    }
    fs::rename(&tmp, path)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Reading back
// ---------------------------------------------------------------------------

/// Load a game's metadata.
pub fn load_meta(dir: &Path) -> Option<GameSessionMeta> {
    let raw = fs::read_to_string(dir.join("meta.json")).ok()?;
    serde_json::from_str(&raw).ok()
}

/// Load a game's samples.
///
/// Malformed lines are skipped rather than aborting the load: a crash mid-write
/// can leave a truncated final line, and losing one sample is much better than
/// losing a whole game's history.
pub fn load_samples(dir: &Path) -> Vec<GameSample> {
    let file = match fs::File::open(dir.join("samples.jsonl")) {
        Ok(f) => f,
        Err(_) => return Vec::new(),
    };
    let mut out = Vec::new();
    let mut skipped = 0usize;
    for line in BufReader::new(file).lines().map_while(Result::ok) {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        match serde_json::from_str::<GameSample>(trimmed) {
            Ok(s) => out.push(s),
            Err(_) => skipped += 1,
        }
    }
    if skipped > 0 {
        warn!(
            "[STORAGE] skipped {skipped} malformed sample line(s) in {}",
            dir.display()
        );
    }
    out
}

/// Enumerate all game directories for a league, oldest first.
pub fn list_game_dirs(data_dir: &str, league_key: &str) -> Vec<PathBuf> {
    let root = league_dir(data_dir, league_key);
    let mut dirs: Vec<PathBuf> = match fs::read_dir(&root) {
        Ok(rd) => rd
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect(),
        Err(_) => Vec::new(),
    };
    // Directory names begin with the ISO date, so lexical order is chronological.
    dirs.sort();
    dirs
}

/// Load every completed game for a league — the sports analog of
/// `engine::load_sessions`.
pub fn load_completed_games(
    data_dir: &str,
    league_key: &str,
    recent: Option<usize>,
) -> Vec<(GameSessionMeta, Vec<GameSample>)> {
    let mut dirs = list_game_dirs(data_dir, league_key);
    if let Some(n) = recent {
        if dirs.len() > n {
            dirs = dirs.split_off(dirs.len() - n);
        }
    }

    let mut out = Vec::new();
    for dir in dirs {
        let meta = match load_meta(&dir) {
            Some(m) => m,
            None => continue,
        };
        // Only resolved games are usable for evaluation.
        if meta.outcome_leg_key.is_none() {
            continue;
        }
        let samples = load_samples(&dir);
        if samples.is_empty() {
            continue;
        }
        out.push((meta, samples));
    }
    out
}

pub fn now_utc() -> String {
    Utc::now().to_rfc3339()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::models::{
        GameState, GameStatus, LegBook, MarketGroup, Sport, SportsLeg, SportsMarketKind,
    };
    use tempfile::tempdir;

    fn group() -> MarketGroup {
        MarketGroup {
            game_id: Some(99),
            league_key: "nfl".into(),
            event_id: "e".into(),
            event_slug: "slug".into(),
            event_title: "Eagles vs Chiefs".into(),
            home_team: "Chiefs".into(),
            away_team: "Eagles".into(),
            start_time_utc: "2026-02-09T23:30:00Z".into(),
            legs: vec![
                SportsLeg {
                    key: "home".into(),
                    label: "Chiefs".into(),
                    token_id: "t-h".into(),
                    condition_id: "c".into(),
                    kind: SportsMarketKind::Moneyline,
                    line: None,
                },
                SportsLeg {
                    key: "away".into(),
                    label: "Eagles".into(),
                    token_id: "t-a".into(),
                    condition_id: "c".into(),
                    kind: SportsMarketKind::Moneyline,
                    line: None,
                },
            ],
        }
    }

    fn meta() -> GameSessionMeta {
        GameSessionMeta {
            group: group(),
            league_key: "nfl".into(),
            sport: Sport::AmericanFootball,
            collection_started_utc: "2026-02-09T23:20:00Z".into(),
            collection_ended_utc: String::new(),
            final_state: None,
            outcome_leg_key: None,
        }
    }

    fn sample(i: usize) -> GameSample {
        GameSample {
            timestamp_utc: format!("2026-02-09T23:{:02}:00Z", 30 + i),
            elapsed_seconds: i as f64 * 60.0,
            state: GameState {
                home_score: i as i32,
                away_score: 0,
                period: "Q1".into(),
                clock: "10:00".into(),
                status: GameStatus::InProgress,
                elapsed_minutes: Some(5.0),
                home_red_cards: 0,
                away_red_cards: 0,
                under_review: false,
            },
            books: vec![
                LegBook {
                    midpoint: Some(0.6),
                    best_bid: Some(0.59),
                    best_ask: Some(0.61),
                    bid_levels: vec![(0.59, 100.0)],
                    ask_levels: vec![(0.61, 120.0)],
                },
                LegBook {
                    midpoint: Some(0.4),
                    ..Default::default()
                },
            ],
            model_probs: vec![0.62, 0.38],
            reference_home_prob: Some(0.61),
        }
    }

    #[test]
    fn appends_and_reads_back_samples_with_depth() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();
        let mut store = GameStore::create(d, "nfl", &meta()).unwrap();

        for i in 0..5 {
            store.append_sample(&sample(i)).unwrap();
        }
        assert_eq!(store.sample_count(), 5);

        let loaded = load_samples(store.dir());
        assert_eq!(loaded.len(), 5);
        assert_eq!(loaded[3].state.home_score, 3);
        // Depth must survive the round trip — the backtest fill model needs it
        // and it cannot be reconstructed later.
        assert_eq!(loaded[0].books[0].bid_levels, vec![(0.59, 100.0)]);
        assert_eq!(loaded[0].books[0].ask_levels, vec![(0.61, 120.0)]);
    }

    #[test]
    fn samples_file_is_appended_not_rewritten() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();
        let mut store = GameStore::create(d, "nfl", &meta()).unwrap();

        store.append_sample(&sample(0)).unwrap();
        let after_one = fs::read_to_string(store.dir().join("samples.jsonl")).unwrap();
        store.append_sample(&sample(1)).unwrap();
        let after_two = fs::read_to_string(store.dir().join("samples.jsonl")).unwrap();

        assert!(
            after_two.starts_with(&after_one),
            "append must preserve prior bytes rather than rewriting the file"
        );
        assert_eq!(after_two.lines().count(), 2);
    }

    #[test]
    fn a_truncated_final_line_does_not_lose_the_whole_game() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();
        let mut store = GameStore::create(d, "nfl", &meta()).unwrap();
        for i in 0..3 {
            store.append_sample(&sample(i)).unwrap();
        }

        // Simulate a crash mid-write.
        let path = store.dir().join("samples.jsonl");
        let mut content = fs::read_to_string(&path).unwrap();
        content.push_str("{\"timestamp_utc\":\"2026-");
        fs::write(&path, content).unwrap();

        let loaded = load_samples(store.dir());
        assert_eq!(loaded.len(), 3, "complete lines must still load");
    }

    #[test]
    fn meta_round_trips() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();
        let store = GameStore::create(d, "nfl", &meta()).unwrap();

        let mut m = meta();
        m.outcome_leg_key = Some("home".into());
        m.collection_ended_utc = "2026-02-10T03:00:00Z".into();
        store.finalize(&m).unwrap();

        let loaded = load_meta(store.dir()).unwrap();
        assert_eq!(loaded.outcome_leg_key.as_deref(), Some("home"));
        assert_eq!(loaded.outcome_leg(), Some(0));
        assert_eq!(loaded.group.game_id, Some(99));
    }

    #[test]
    fn unresolved_games_are_excluded_from_the_completed_set() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();

        // Unresolved.
        let mut s1 = GameStore::create(d, "nfl", &meta()).unwrap();
        s1.append_sample(&sample(0)).unwrap();

        // Resolved.
        let mut m2 = meta();
        m2.group.game_id = Some(100);
        m2.group.start_time_utc = "2026-02-16T23:30:00Z".into();
        m2.outcome_leg_key = Some("away".into());
        let mut s2 = GameStore::create(d, "nfl", &m2).unwrap();
        s2.append_sample(&sample(1)).unwrap();

        let completed = load_completed_games(d, "nfl", None);
        assert_eq!(completed.len(), 1);
        assert_eq!(completed[0].0.group.game_id, Some(100));
    }

    #[test]
    fn recent_limits_to_the_newest_games_chronologically() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();

        for (i, date) in ["2026-01-01", "2026-02-01", "2026-03-01"].iter().enumerate() {
            let mut m = meta();
            m.group.game_id = Some(i as i64);
            m.group.start_time_utc = format!("{date}T20:00:00Z");
            m.outcome_leg_key = Some("home".into());
            let mut s = GameStore::create(d, "nfl", &m).unwrap();
            s.append_sample(&sample(0)).unwrap();
        }

        let recent = load_completed_games(d, "nfl", Some(2));
        assert_eq!(recent.len(), 2);
        assert!(recent[0].0.group.start_time_utc.starts_with("2026-02-01"));
        assert!(recent[1].0.group.start_time_utc.starts_with("2026-03-01"));
    }

    #[test]
    fn decisions_and_trades_go_to_separate_streams() {
        let tmp = tempdir().unwrap();
        let d = tmp.path().to_str().unwrap();
        let store = GameStore::create(d, "nfl", &meta()).unwrap();

        store
            .append_decision(&serde_json::json!({"gate": "min_edge", "edge": 0.01}))
            .unwrap();
        store
            .append_trade(&serde_json::json!({"side": "buy_home", "price": 0.61}))
            .unwrap();

        let decisions = fs::read_to_string(store.dir().join("decisions.jsonl")).unwrap();
        let trades = fs::read_to_string(store.dir().join("trades.jsonl")).unwrap();
        assert!(decisions.contains("min_edge"));
        assert!(trades.contains("buy_home"));
        assert!(!trades.contains("min_edge"));
    }
}
