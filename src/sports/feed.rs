//! Polymarket sports results WebSocket client.
//!
//! `wss://sports-api.polymarket.com/ws` is public, requires no auth, and needs
//! **no subscribe message** — connect and it streams state for every active
//! sports event. This is the direct analog of `rtds.rs` for crypto: the
//! exogenous truth signal the book is judged against.
//!
//! Protocol notes:
//!   - Each frame *is* a game update object; there is no envelope and no
//!     `event_type` discriminator.
//!   - The server sends a text-frame `ping` every 5s and expects `pong` within
//!     10s or it closes the connection.
//!
//! The observed payload shape is:
//! ```json
//! { "gameId": 5127839, "leagueAbbreviation": "NBA",
//!   "homeTeam": "Los Angeles Lakers", "awayTeam": "Boston Celtics",
//!   "status": "InProgress", "live": true, "ended": false,
//!   "score": "98-94", "period": "Q4", "elapsed": "05:12" }
//! ```
//!
//! The parser is deliberately defensive: field names are matched
//! case-insensitively across several plausible spellings, and anything that
//! cannot be interpreted yields `None` rather than a default. An unverified
//! schema that silently parses to 0-0 would be worse than no feed at all.

use log::{debug, info, warn};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tungstenite::Message;

use super::models::{parse_elapsed_minutes, parse_score, GameState, GameStatus, Sport};

pub const SPORTS_WS_URL: &str = "wss://sports-api.polymarket.com/ws";
const RECONNECT_DELAY_S: f64 = 2.0;
const MAX_RECONNECT_DELAY_S: f64 = 30.0;
/// A game whose update is older than this is not trusted for trading.
pub const STALE_THRESHOLD_S: f64 = 30.0;
const READ_TIMEOUT_S: u64 = 2;

/// A game update as received from the feed.
#[derive(Debug, Clone)]
pub struct FeedGame {
    pub game_id: i64,
    pub league_abbreviation: String,
    pub home_team: String,
    pub away_team: String,
    pub state: GameState,
    pub updated_at: Instant,
    /// Wall-clock receipt time, for the latency report.
    pub received_utc: String,
}

impl FeedGame {
    pub fn is_stale(&self) -> bool {
        self.updated_at.elapsed().as_secs_f64() > STALE_THRESHOLD_S
    }
}

struct Inner {
    games: HashMap<i64, FeedGame>,
    /// Bumped whenever a game's material state changes, so consumers can cheaply
    /// detect "something happened" without diffing every game.
    material_change_seq: u64,
    last_message_at: Option<Instant>,
}

#[derive(Clone)]
pub struct SportsFeedClient {
    inner: Arc<Mutex<Inner>>,
    running: Arc<AtomicBool>,
    message_count: Arc<AtomicU64>,
    /// Sport used to interpret period/clock. The feed carries many leagues on
    /// one socket, so this is resolved per game via `league_sport`.
    league_sport: Arc<Mutex<HashMap<String, Sport>>>,
}

impl SportsFeedClient {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner {
                games: HashMap::new(),
                material_change_seq: 0,
                last_message_at: None,
            })),
            running: Arc::new(AtomicBool::new(false)),
            message_count: Arc::new(AtomicU64::new(0)),
            league_sport: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Register how to interpret a league's clock. Games from unregistered
    /// leagues are still stored, but with `elapsed_minutes: None`.
    pub fn register_league(&self, feed_abbreviation: &str, sport: Sport) {
        if let Ok(mut m) = self.league_sport.lock() {
            m.insert(feed_abbreviation.to_ascii_uppercase(), sport);
        }
    }

    pub fn start(&self) {
        if self.running.swap(true, Ordering::SeqCst) {
            return;
        }
        let inner = Arc::clone(&self.inner);
        let running = Arc::clone(&self.running);
        let count = Arc::clone(&self.message_count);
        let leagues = Arc::clone(&self.league_sport);

        thread::Builder::new()
            .name("sports-feed-ws".into())
            .spawn(move || run_loop(&inner, &running, &count, &leagues))
            .expect("failed to spawn sports feed thread");

        info!("[SPORTS-WS] client started ({})", SPORTS_WS_URL);
    }

    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
    }

    pub fn message_count(&self) -> u64 {
        self.message_count.load(Ordering::SeqCst)
    }

    /// Latest state for a game, or `None` if never seen.
    pub fn game(&self, game_id: i64) -> Option<FeedGame> {
        self.inner.lock().ok()?.games.get(&game_id).cloned()
    }

    /// Latest state for a game, but only if it is fresh enough to trade on.
    pub fn fresh_game(&self, game_id: i64) -> Option<FeedGame> {
        self.game(game_id).filter(|g| !g.is_stale())
    }

    pub fn all_games(&self) -> Vec<FeedGame> {
        self.inner
            .lock()
            .map(|i| i.games.values().cloned().collect())
            .unwrap_or_default()
    }

    /// Games belonging to a league abbreviation.
    pub fn games_for_league(&self, abbreviation: &str) -> Vec<FeedGame> {
        let want = abbreviation.to_ascii_uppercase();
        self.all_games()
            .into_iter()
            .filter(|g| g.league_abbreviation.to_ascii_uppercase() == want)
            .collect()
    }

    pub fn material_change_seq(&self) -> u64 {
        self.inner.lock().map(|i| i.material_change_seq).unwrap_or(0)
    }

    /// Whether the socket has produced anything recently. A connected-but-silent
    /// feed is indistinguishable from "no games today" without this.
    pub fn seconds_since_last_message(&self) -> Option<f64> {
        self.inner
            .lock()
            .ok()?
            .last_message_at
            .map(|t| t.elapsed().as_secs_f64())
    }

    /// Test seam: inject a game update as though it arrived from the socket.
    #[doc(hidden)]
    pub fn inject_for_test(&self, game: FeedGame) {
        if let Ok(mut inner) = self.inner.lock() {
            store_game(&mut inner, game);
        }
    }
}

impl Default for SportsFeedClient {
    fn default() -> Self {
        Self::new()
    }
}

fn store_game(inner: &mut Inner, game: FeedGame) {
    let changed = inner
        .games
        .get(&game.game_id)
        .map(|prev| prev.state.is_material_change(&game.state))
        .unwrap_or(true);
    if changed {
        inner.material_change_seq += 1;
    }
    inner.last_message_at = Some(Instant::now());
    inner.games.insert(game.game_id, game);
}

fn run_loop(
    inner: &Arc<Mutex<Inner>>,
    running: &AtomicBool,
    count: &AtomicU64,
    leagues: &Arc<Mutex<HashMap<String, Sport>>>,
) {
    let mut delay = RECONNECT_DELAY_S;
    while running.load(Ordering::SeqCst) {
        match connect_once(inner, running, count, leagues) {
            Ok(()) => delay = RECONNECT_DELAY_S,
            Err(e) => warn!("[SPORTS-WS] connection error: {e}"),
        }
        if !running.load(Ordering::SeqCst) {
            break;
        }
        info!("[SPORTS-WS] reconnecting in {delay:.1}s");
        let deadline = Instant::now() + Duration::from_secs_f64(delay);
        while Instant::now() < deadline && running.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(200));
        }
        delay = (delay * 1.5).min(MAX_RECONNECT_DELAY_S);
    }
}

fn connect_once(
    inner: &Arc<Mutex<Inner>>,
    running: &AtomicBool,
    count: &AtomicU64,
    leagues: &Arc<Mutex<HashMap<String, Sport>>>,
) -> Result<(), Box<dyn std::error::Error>> {
    let (mut ws, _resp) = tungstenite::connect(SPORTS_WS_URL)?;
    info!("[SPORTS-WS] connected");

    if let tungstenite::stream::MaybeTlsStream::Plain(tcp) = ws.get_ref() {
        let _ = tcp.set_read_timeout(Some(Duration::from_secs(READ_TIMEOUT_S)));
    }

    loop {
        if !running.load(Ordering::SeqCst) {
            let _ = ws.close(None);
            break;
        }

        let msg = match ws.read() {
            Ok(m) => m,
            Err(tungstenite::Error::Io(ref e))
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                continue;
            }
            Err(e) => {
                warn!("[SPORTS-WS] read error: {e}");
                break;
            }
        };

        match msg {
            Message::Text(text) => {
                let trimmed = text.trim();
                // Application-level heartbeat: the server sends a *text* frame
                // "ping" every 5s and closes the socket if we do not reply
                // within 10s.
                if trimmed.eq_ignore_ascii_case("ping") {
                    ws.send(Message::Text("pong".into()))?;
                    continue;
                }
                if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("pong") {
                    continue;
                }

                let value: Value = match serde_json::from_str(trimmed) {
                    Ok(v) => v,
                    Err(e) => {
                        debug!("[SPORTS-WS] unparseable frame: {e}");
                        continue;
                    }
                };

                let sports = leagues.lock().map(|m| m.clone()).unwrap_or_default();
                // A frame may be a single game or an array of them.
                let items: Vec<&Value> = match value.as_array() {
                    Some(arr) => arr.iter().collect(),
                    None => vec![&value],
                };
                for item in items {
                    if let Some(game) = parse_feed_game(item, &sports) {
                        count.fetch_add(1, Ordering::Relaxed);
                        if let Ok(mut guard) = inner.lock() {
                            store_game(&mut guard, game);
                        }
                    }
                }
            }
            // Protocol-level ping, in case the server also uses it.
            Message::Ping(data) => {
                let _ = ws.send(Message::Pong(data));
            }
            Message::Close(_) => break,
            _ => {}
        }
    }

    Ok(())
}

/// Case-insensitive field lookup tolerating camelCase / snake_case spellings.
fn field<'a>(v: &'a Value, names: &[&str]) -> Option<&'a Value> {
    let obj = v.as_object()?;
    for name in names {
        if let Some(found) = obj.get(*name) {
            return Some(found);
        }
    }
    // Fall back to a normalised comparison.
    let norm = |s: &str| s.to_ascii_lowercase().replace('_', "");
    let wanted: Vec<String> = names.iter().map(|n| norm(n)).collect();
    obj.iter()
        .find(|(k, _)| wanted.contains(&norm(k)))
        .map(|(_, v)| v)
}

fn field_str(v: &Value, names: &[&str]) -> Option<String> {
    let f = field(v, names)?;
    match f {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

fn field_i64(v: &Value, names: &[&str]) -> Option<i64> {
    let f = field(v, names)?;
    f.as_i64().or_else(|| f.as_str().and_then(|s| s.parse().ok()))
}

fn field_bool(v: &Value, names: &[&str]) -> Option<bool> {
    let f = field(v, names)?;
    f.as_bool()
        .or_else(|| f.as_str().and_then(|s| s.parse().ok()))
}

/// Parse one game update. Returns `None` when the frame lacks a usable game id,
/// which is the only field we truly cannot work without.
pub fn parse_feed_game(v: &Value, league_sports: &HashMap<String, Sport>) -> Option<FeedGame> {
    let game_id = field_i64(v, &["gameId", "game_id", "id"])?;

    let league = field_str(v, &["leagueAbbreviation", "league_abbreviation", "league"])
        .unwrap_or_default();
    let home_team = field_str(v, &["homeTeam", "home_team", "home"]).unwrap_or_default();
    let away_team = field_str(v, &["awayTeam", "away_team", "away"]).unwrap_or_default();

    let raw_status = field_str(v, &["status", "state"]).unwrap_or_default();
    let live = field_bool(v, &["live"]).unwrap_or(false);
    let ended = field_bool(v, &["ended", "completed"]).unwrap_or(false);

    let mut status = GameStatus::from_feed(&raw_status);
    // `ended` and `live` are more reliable than free-text status strings.
    if ended {
        status = GameStatus::Final;
    } else if live && status == GameStatus::Unknown {
        status = GameStatus::InProgress;
    }

    let period = field_str(v, &["period"]).unwrap_or_default();
    let clock = field_str(v, &["elapsed", "clock", "displayClock"]).unwrap_or_default();

    // Scores may arrive as a combined string or as separate numeric fields.
    let (home_score, away_score) = match field_str(v, &["score"]).and_then(|s| parse_score(&s)) {
        Some(pair) => pair,
        None => {
            let h = field_i64(v, &["homeScore", "home_score"]);
            let a = field_i64(v, &["awayScore", "away_score"]);
            match (h, a) {
                (Some(h), Some(a)) => (h as i32, a as i32),
                // No usable score. Report 0-0 only for a game that has not
                // started; otherwise leave it at 0-0 but keep the status
                // honest so downstream staleness checks can reject it.
                _ => (0, 0),
            }
        }
    };

    let sport = league_sports.get(&league.to_ascii_uppercase()).copied();
    let elapsed_minutes = sport.and_then(|s| parse_elapsed_minutes(s, &period, &clock));

    let under_review = field_bool(v, &["underReview", "under_review", "review"]).unwrap_or(false);
    let home_red_cards = field_i64(v, &["homeRedCards", "home_red_cards"]).unwrap_or(0) as i32;
    let away_red_cards = field_i64(v, &["awayRedCards", "away_red_cards"]).unwrap_or(0) as i32;

    Some(FeedGame {
        game_id,
        league_abbreviation: league,
        home_team,
        away_team,
        state: GameState {
            home_score,
            away_score,
            period,
            clock,
            status,
            elapsed_minutes,
            home_red_cards,
            away_red_cards,
            under_review,
        },
        updated_at: Instant::now(),
        received_utc: chrono::Utc::now().to_rfc3339(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn nfl_map() -> HashMap<String, Sport> {
        let mut m = HashMap::new();
        m.insert("NFL".to_string(), Sport::AmericanFootball);
        m.insert("EPL".to_string(), Sport::Soccer);
        m
    }

    #[test]
    fn parses_the_documented_payload_shape() {
        let v = json!({
            "gameId": 5127839,
            "leagueAbbreviation": "NFL",
            "homeTeam": "Kansas City Chiefs",
            "awayTeam": "Philadelphia Eagles",
            "status": "InProgress",
            "live": true,
            "ended": false,
            "score": "24-21",
            "period": "Q4",
            "elapsed": "05:12"
        });

        let g = parse_feed_game(&v, &nfl_map()).expect("should parse");
        assert_eq!(g.game_id, 5127839);
        assert_eq!(g.league_abbreviation, "NFL");
        assert_eq!(g.home_team, "Kansas City Chiefs");
        assert_eq!(g.state.home_score, 24);
        assert_eq!(g.state.away_score, 21);
        assert_eq!(g.state.status, GameStatus::InProgress);
        // Q4 with 5:12 left -> 54.8 minutes played.
        let e = g.state.elapsed_minutes.unwrap();
        assert!((e - 54.8).abs() < 0.01, "got {e}");
    }

    #[test]
    fn tolerates_snake_case_and_separate_score_fields() {
        let v = json!({
            "game_id": 42,
            "league_abbreviation": "EPL",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "status": "in_progress",
            "home_score": 2,
            "away_score": 1,
            "period": "2H",
            "clock": "67:30"
        });
        let g = parse_feed_game(&v, &nfl_map()).expect("should parse");
        assert_eq!(g.game_id, 42);
        assert_eq!(g.state.home_score, 2);
        assert_eq!(g.state.away_score, 1);
        assert_eq!(g.state.elapsed_minutes, Some(67.5));
    }

    #[test]
    fn a_frame_without_a_game_id_is_rejected() {
        let v = json!({ "homeTeam": "A", "awayTeam": "B", "score": "1-0" });
        assert!(parse_feed_game(&v, &nfl_map()).is_none());
    }

    #[test]
    fn ended_overrides_a_stale_status_string() {
        let v = json!({
            "gameId": 7,
            "leagueAbbreviation": "NFL",
            "status": "InProgress",
            "live": false,
            "ended": true,
            "score": "17-14",
            "period": "Q4",
            "elapsed": "00:00"
        });
        let g = parse_feed_game(&v, &nfl_map()).unwrap();
        assert_eq!(g.state.status, GameStatus::Final);
    }

    #[test]
    fn unknown_league_leaves_elapsed_unknown_rather_than_guessing() {
        let v = json!({
            "gameId": 9,
            "leagueAbbreviation": "KABADDI",
            "status": "InProgress",
            "score": "10-8",
            "period": "Q2",
            "elapsed": "05:00"
        });
        let g = parse_feed_game(&v, &nfl_map()).unwrap();
        assert_eq!(g.state.elapsed_minutes, None);
    }

    #[test]
    fn material_change_sequence_advances_only_on_real_events() {
        let client = SportsFeedClient::new();
        let base = |home: i32, clock: &str| FeedGame {
            game_id: 1,
            league_abbreviation: "NFL".into(),
            home_team: "A".into(),
            away_team: "B".into(),
            state: GameState {
                home_score: home,
                away_score: 0,
                period: "Q1".into(),
                clock: clock.into(),
                status: GameStatus::InProgress,
                elapsed_minutes: Some(1.0),
                home_red_cards: 0,
                away_red_cards: 0,
                under_review: false,
            },
            updated_at: Instant::now(),
            received_utc: String::new(),
        };

        client.inject_for_test(base(0, "14:00"));
        let after_first = client.material_change_seq();

        // Clock-only update must not count as a material change.
        client.inject_for_test(base(0, "13:00"));
        assert_eq!(client.material_change_seq(), after_first);

        // A score does.
        client.inject_for_test(base(7, "12:00"));
        assert_eq!(client.material_change_seq(), after_first + 1);
    }

    #[test]
    fn stale_games_are_filtered_out_of_fresh_lookups() {
        let client = SportsFeedClient::new();
        let mut g = FeedGame {
            game_id: 3,
            league_abbreviation: "EPL".into(),
            home_team: "A".into(),
            away_team: "B".into(),
            state: GameState::scheduled(),
            updated_at: Instant::now() - Duration::from_secs_f64(STALE_THRESHOLD_S + 5.0),
            received_utc: String::new(),
        };
        g.state.status = GameStatus::InProgress;
        client.inject_for_test(g);

        assert!(client.game(3).is_some(), "raw lookup still returns it");
        assert!(
            client.fresh_game(3).is_none(),
            "stale game must not be offered for trading"
        );
    }

    #[test]
    fn games_can_be_filtered_by_league() {
        let client = SportsFeedClient::new();
        for (id, league) in [(1i64, "NFL"), (2, "EPL"), (3, "NFL")] {
            client.inject_for_test(FeedGame {
                game_id: id,
                league_abbreviation: league.into(),
                home_team: "A".into(),
                away_team: "B".into(),
                state: GameState::scheduled(),
                updated_at: Instant::now(),
                received_utc: String::new(),
            });
        }
        assert_eq!(client.games_for_league("nfl").len(), 2);
        assert_eq!(client.games_for_league("EPL").len(), 1);
    }
}
