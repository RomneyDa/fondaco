//! ESPN secondary feed.
//!
//! Two jobs:
//!   1. **Cross-confirmation** of scores, so a retracted goal or a reversed
//!      touchdown does not get traded on a single unconfirmed report.
//!   2. **NFL live win probability**, which ESPN publishes free and
//!      unauthenticated and which is nflfastR-class — better than anything we
//!      would fit quickly. There is no soccer equivalent; soccer uses the
//!      in-house model in `fair_value`.
//!
//! Endpoints (unofficial, undocumented, no auth):
//!   - `site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard`
//!   - `site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={id}`
//!   - `sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{id}/competitions/{id}/probabilities`
//!
//! ESPN can change or remove these without notice, so every failure is soft:
//! a missing ESPN response degrades confirmation and reference probability, it
//! never blocks collection.

use std::collections::HashMap;
use std::time::Duration;

use log::{debug, warn};
use reqwest::blocking::Client;
use serde_json::Value;

use super::models::{parse_elapsed_minutes, GameState, GameStatus, League, Sport};

pub const ESPN_SITE_BASE: &str = "https://site.api.espn.com/apis/site/v2/sports";
pub const ESPN_CORE_BASE: &str = "https://sports.core.api.espn.com/v2/sports";
const TIMEOUT_S: u64 = 8;

/// One game as ESPN sees it.
#[derive(Debug, Clone)]
pub struct EspnGame {
    pub espn_event_id: String,
    pub home_team: String,
    pub away_team: String,
    pub state: GameState,
    /// ESPN's live win probability for the home team, when published.
    pub home_win_probability: Option<f64>,
}

fn get_json(client: &Client, url: &str) -> Option<Value> {
    let resp = client
        .get(url)
        .timeout(Duration::from_secs(TIMEOUT_S))
        .send()
        .ok()?;
    if !resp.status().is_success() {
        debug!("[ESPN] {url} -> HTTP {}", resp.status());
        return None;
    }
    resp.json().ok()
}

/// Fetch the scoreboard for a league.
pub fn fetch_scoreboard(client: &Client, league: &League) -> Vec<EspnGame> {
    let url = format!(
        "{ESPN_SITE_BASE}/{}/{}/scoreboard",
        league.espn_sport, league.espn_league
    );
    let json = match get_json(client, &url) {
        Some(j) => j,
        None => {
            warn!("[ESPN] scoreboard unavailable for {}", league.key);
            return Vec::new();
        }
    };
    parse_scoreboard(&json, league.sport)
}

/// Parse an ESPN scoreboard payload.
pub fn parse_scoreboard(json: &Value, sport: Sport) -> Vec<EspnGame> {
    let events = match json.get("events").and_then(|e| e.as_array()) {
        Some(e) => e,
        None => return Vec::new(),
    };

    let mut out = Vec::new();
    for event in events {
        if let Some(g) = parse_event(event, sport) {
            out.push(g);
        }
    }
    out
}

fn parse_event(event: &Value, sport: Sport) -> Option<EspnGame> {
    let espn_event_id = event.get("id").and_then(|v| v.as_str())?.to_string();
    let competition = event
        .get("competitions")
        .and_then(|c| c.as_array())
        .and_then(|a| a.first())?;

    let competitors = competition.get("competitors")?.as_array()?;

    let mut home_team = String::new();
    let mut away_team = String::new();
    let mut home_score = 0i32;
    let mut away_score = 0i32;
    let mut home_red_cards = 0i32;
    let mut away_red_cards = 0i32;

    for c in competitors {
        let is_home = c.get("homeAway").and_then(|v| v.as_str()) == Some("home");
        let name = c
            .get("team")
            .and_then(|t| {
                t.get("displayName")
                    .or_else(|| t.get("name"))
                    .or_else(|| t.get("shortDisplayName"))
            })
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let score = c
            .get("score")
            .and_then(|v| {
                v.as_i64()
                    .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            })
            .unwrap_or(0) as i32;
        // Soccer red cards appear as a competitor-level statistic.
        let reds = c
            .get("statistics")
            .and_then(|s| s.as_array())
            .and_then(|arr| {
                arr.iter()
                    .find(|st| {
                        st.get("name")
                            .and_then(|n| n.as_str())
                            .map(|n| n.eq_ignore_ascii_case("redCards"))
                            .unwrap_or(false)
                    })
                    .and_then(|st| st.get("displayValue").or_else(|| st.get("value")))
                    .and_then(|v| {
                        v.as_i64()
                            .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
                    })
            })
            .unwrap_or(0) as i32;

        if is_home {
            home_team = name;
            home_score = score;
            home_red_cards = reds;
        } else {
            away_team = name;
            away_score = score;
            away_red_cards = reds;
        }
    }

    let status_node = competition.get("status").or_else(|| event.get("status"));
    let raw_status = status_node
        .and_then(|s| s.get("type"))
        .and_then(|t| {
            t.get("name")
                .or_else(|| t.get("state"))
                .or_else(|| t.get("description"))
        })
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let status = normalize_espn_status(raw_status);

    let period = status_node
        .and_then(|s| s.get("period"))
        .and_then(|v| v.as_i64())
        .map(|p| match sport {
            Sport::AmericanFootball => format!("Q{p}"),
            Sport::Soccer => format!("{p}H"),
        })
        .unwrap_or_default();
    let clock = status_node
        .and_then(|s| s.get("displayClock"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    let elapsed_minutes = parse_elapsed_minutes(sport, &period, &clock);

    Some(EspnGame {
        espn_event_id,
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
            under_review: false,
        },
        home_win_probability: None,
    })
}

fn normalize_espn_status(raw: &str) -> GameStatus {
    let s = raw.to_ascii_lowercase();
    if s.contains("final") {
        GameStatus::Final
    } else if s.contains("halftime") {
        GameStatus::Halftime
    } else if s.contains("in") && s.contains("progress") {
        GameStatus::InProgress
    } else if s.contains("scheduled") || s.contains("pre") {
        GameStatus::Scheduled
    } else if s.contains("delay") || s.contains("postpon") || s.contains("suspend") {
        GameStatus::Suspended
    } else if s == "status_in_progress" || s == "in" {
        GameStatus::InProgress
    } else {
        GameStatus::Unknown
    }
}

/// Fetch ESPN's live win probability for the home team of an NFL game.
///
/// Returns `None` for soccer (ESPN publishes no equivalent) and whenever the
/// endpoint is unavailable or the payload has no probability array.
pub fn fetch_home_win_probability(
    client: &Client,
    league: &League,
    espn_event_id: &str,
) -> Option<f64> {
    if league.sport != Sport::AmericanFootball {
        return None;
    }

    let url = format!(
        "{ESPN_SITE_BASE}/{}/{}/summary?event={espn_event_id}",
        league.espn_sport, league.espn_league
    );
    let json = get_json(client, &url)?;
    parse_win_probability(&json)
}

/// Extract the most recent home win probability from a `summary` payload.
pub fn parse_win_probability(json: &Value) -> Option<f64> {
    let arr = json.get("winprobability")?.as_array()?;
    // The array is chronological; the last entry is the current state.
    let last = arr.last()?;
    let p = last
        .get("homeWinPercentage")
        .or_else(|| last.get("homeWinProbability"))
        .and_then(|v| v.as_f64())?;
    // ESPN publishes this as a fraction in [0, 1], but has used percentages in
    // places. Normalise rather than trusting either.
    let p = if p > 1.0 { p / 100.0 } else { p };
    if (0.0..=1.0).contains(&p) {
        Some(p)
    } else {
        None
    }
}

/// Match ESPN games to our teams, keyed by feed game id via the caller's map.
pub fn index_by_teams(games: Vec<EspnGame>) -> HashMap<(String, String), EspnGame> {
    games
        .into_iter()
        .map(|g| {
            (
                (
                    g.home_team.to_ascii_lowercase(),
                    g.away_team.to_ascii_lowercase(),
                ),
                g,
            )
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_an_nfl_scoreboard_entry() {
        let json = json!({
            "events": [{
                "id": "401547",
                "competitions": [{
                    "status": {
                        "period": 4,
                        "displayClock": "5:12",
                        "type": { "name": "STATUS_IN_PROGRESS" }
                    },
                    "competitors": [
                        { "homeAway": "home", "score": "24",
                          "team": { "displayName": "Kansas City Chiefs" } },
                        { "homeAway": "away", "score": "21",
                          "team": { "displayName": "Philadelphia Eagles" } }
                    ]
                }]
            }]
        });

        let games = parse_scoreboard(&json, Sport::AmericanFootball);
        assert_eq!(games.len(), 1);
        let g = &games[0];
        assert_eq!(g.espn_event_id, "401547");
        assert_eq!(g.home_team, "Kansas City Chiefs");
        assert_eq!(g.state.home_score, 24);
        assert_eq!(g.state.away_score, 21);
        assert_eq!(g.state.status, GameStatus::InProgress);
        let e = g.state.elapsed_minutes.unwrap();
        assert!((e - 54.8).abs() < 0.01, "got {e}");
    }

    #[test]
    fn parses_soccer_with_red_cards() {
        let json = json!({
            "events": [{
                "id": "700",
                "competitions": [{
                    "status": {
                        "period": 2,
                        "displayClock": "67:30",
                        "type": { "name": "STATUS_IN_PROGRESS" }
                    },
                    "competitors": [
                        { "homeAway": "home", "score": "2",
                          "team": { "displayName": "Arsenal" },
                          "statistics": [{ "name": "redCards", "displayValue": "1" }] },
                        { "homeAway": "away", "score": "1",
                          "team": { "displayName": "Chelsea" },
                          "statistics": [] }
                    ]
                }]
            }]
        });

        let games = parse_scoreboard(&json, Sport::Soccer);
        let g = &games[0];
        assert_eq!(g.state.home_score, 2);
        assert_eq!(g.state.home_red_cards, 1);
        assert_eq!(g.state.away_red_cards, 0);
        assert_eq!(g.state.elapsed_minutes, Some(67.5));
    }

    #[test]
    fn status_strings_normalise() {
        assert_eq!(
            normalize_espn_status("STATUS_IN_PROGRESS"),
            GameStatus::InProgress
        );
        assert_eq!(normalize_espn_status("STATUS_FINAL"), GameStatus::Final);
        assert_eq!(normalize_espn_status("STATUS_HALFTIME"), GameStatus::Halftime);
        assert_eq!(
            normalize_espn_status("STATUS_SCHEDULED"),
            GameStatus::Scheduled
        );
        assert_eq!(
            normalize_espn_status("STATUS_POSTPONED"),
            GameStatus::Suspended
        );
    }

    #[test]
    fn win_probability_takes_the_latest_entry() {
        let json = json!({
            "winprobability": [
                { "homeWinPercentage": 0.50 },
                { "homeWinPercentage": 0.61 },
                { "homeWinPercentage": 0.73 }
            ]
        });
        assert_eq!(parse_win_probability(&json), Some(0.73));
    }

    #[test]
    fn win_probability_normalises_percentages() {
        let json = json!({ "winprobability": [{ "homeWinPercentage": 73.0 }] });
        assert_eq!(parse_win_probability(&json), Some(0.73));
    }

    #[test]
    fn win_probability_absent_or_nonsense_yields_none() {
        assert_eq!(parse_win_probability(&json!({})), None);
        assert_eq!(parse_win_probability(&json!({"winprobability": []})), None);
        assert_eq!(
            parse_win_probability(&json!({"winprobability": [{"homeWinPercentage": 5000.0}]})),
            None,
            "an out-of-range value must be rejected, not clamped"
        );
    }

    #[test]
    fn an_empty_scoreboard_is_handled() {
        assert!(parse_scoreboard(&json!({}), Sport::Soccer).is_empty());
        assert!(parse_scoreboard(&json!({"events": []}), Sport::Soccer).is_empty());
    }
}
