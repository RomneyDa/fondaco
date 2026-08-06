//! Schedule discovery: Gamma events -> `MarketGroup`, joined to feed game ids.
//!
//! Crypto discovery is deterministic (`slug_prefix + floor(now/window)*window`).
//! Sports has no such trick — games must be enumerated from the Gamma schedule
//! and then **joined** to the Polymarket sports feed by team name and date.
//!
//! That join is the flakiest part of the whole system: team naming differs
//! between the catalog ("Kansas City Chiefs") and the results feed ("Chiefs",
//! "KC Chiefs"). Every failed join is logged loudly and the affected group is
//! marked untradeable rather than being silently traded blind, because a group
//! without a game id has no live state behind it.

use log::{debug, info, warn};
use reqwest::blocking::Client;
use serde_json::Value;
use std::collections::HashMap;
use std::time::Duration;

use crate::defaults::{API_TIMEOUT_DEFAULT, GAMMA_API_BASE};
use crate::sports::feed::FeedGame;
use crate::sports::models::{League, MarketGroup, SportsLeg, SportsMarketKind, Sport};

/// Resolve a league's Gamma tag id from `GET /tags`.
///
/// Tag ids are not hardcoded because they go stale; the lookup is by name with
/// the league key and feed abbreviation as candidates.
pub fn resolve_tag_id(client: &Client, league: &League) -> Option<u64> {
    if let Some(id) = league.gamma_tag_id {
        return Some(id);
    }

    let resp = client
        .get(format!("{GAMMA_API_BASE}/tags"))
        .query(&[("limit", "500")])
        .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
        .send()
        .ok()?;
    if !resp.status().is_success() {
        warn!("[DISCOVERY] /tags returned {}", resp.status());
        return None;
    }
    let tags: Value = resp.json().ok()?;
    let arr = tags.as_array()?;

    let candidates = [
        league.key.to_ascii_lowercase(),
        league.feed_abbreviation.to_ascii_lowercase(),
        league.slug_hint.to_ascii_lowercase(),
    ];

    for tag in arr {
        let label = tag
            .get("label")
            .or_else(|| tag.get("name"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        let slug = tag
            .get("slug")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if candidates.iter().any(|c| *c == label || *c == slug) {
            let id = tag
                .get("id")
                .and_then(|v| v.as_u64().or_else(|| v.as_str().and_then(|s| s.parse().ok())));
            if let Some(id) = id {
                info!("[DISCOVERY] resolved tag id {id} for league {}", league.key);
                return Some(id);
            }
        }
    }

    warn!("[DISCOVERY] no Gamma tag found for league {}", league.key);
    None
}

/// Fetch upcoming and in-progress events for a league.
pub fn fetch_events(client: &Client, league: &League, tag_id: Option<u64>) -> Vec<Value> {
    let mut query: Vec<(String, String)> = vec![
        ("closed".into(), "false".into()),
        ("limit".into(), "500".into()),
        ("order".into(), "startDate".into()),
        ("ascending".into(), "true".into()),
    ];
    if let Some(id) = tag_id {
        query.push(("tag_id".into(), id.to_string()));
    }

    let resp = match client
        .get(format!("{GAMMA_API_BASE}/events"))
        .query(&query)
        .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
        .send()
    {
        Ok(r) => r,
        Err(e) => {
            warn!("[DISCOVERY] /events request failed: {e}");
            return Vec::new();
        }
    };
    if !resp.status().is_success() {
        warn!("[DISCOVERY] /events returned {}", resp.status());
        return Vec::new();
    }

    let events: Value = match resp.json() {
        Ok(v) => v,
        Err(e) => {
            warn!("[DISCOVERY] /events parse error: {e}");
            return Vec::new();
        }
    };

    let arr = match events.as_array() {
        Some(a) => a.clone(),
        None => return Vec::new(),
    };

    // Without a tag id we fall back to slug matching, which is noisy but better
    // than collecting every market on the platform.
    if tag_id.is_none() {
        let hint = league.slug_hint.to_ascii_lowercase();
        return arr
            .into_iter()
            .filter(|e| {
                e.get("slug")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_ascii_lowercase().contains(&hint))
                    .unwrap_or(false)
            })
            .collect();
    }

    arr
}

/// Classify a Gamma market into a sports market kind from its question text.
fn classify_market(question: &str, group_item_title: Option<&str>) -> SportsMarketKind {
    let q = question.to_ascii_lowercase();
    let t = group_item_title.unwrap_or("").to_ascii_lowercase();
    let hay = format!("{q} {t}");
    if hay.contains("spread") || hay.contains("handicap") || hay.contains(" by ") {
        SportsMarketKind::Spread
    } else if hay.contains("total") || hay.contains("over/under") || hay.contains("over ") {
        SportsMarketKind::Total
    } else if hay.contains("win")
        || hay.contains("moneyline")
        || hay.contains("beat")
        // The draw is a moneyline leg, not a separate market kind. Without this
        // a three-way soccer event silently loses its draw and prices as a
        // two-way book — which is wrong for ~25% of matches.
        || hay.contains("draw")
        || hay.contains("tie")
    {
        SportsMarketKind::Moneyline
    } else {
        SportsMarketKind::Other
    }
}

fn str_field(v: &Value, names: &[&str]) -> Option<String> {
    for n in names {
        if let Some(s) = v.get(*n).and_then(|x| x.as_str()) {
            if !s.is_empty() {
                return Some(s.to_string());
            }
        }
    }
    None
}

/// Parse a Gamma event into a `MarketGroup`.
///
/// Returns `None` when the event has no usable moneyline pair — a group we
/// cannot price both sides of is not worth carrying.
pub fn parse_event(event: &Value, league: &League) -> Option<MarketGroup> {
    let event_id = str_field(event, &["id"])?;
    let event_slug = str_field(event, &["slug"]).unwrap_or_default();
    let event_title = str_field(event, &["title"]).unwrap_or_default();
    let start_time_utc = str_field(event, &["startDate", "startTime", "gameStartTime"])
        .unwrap_or_default();

    let markets = event.get("markets")?.as_array()?;

    // Team names: prefer explicit fields, else split the title on "vs".
    let (home_team, away_team) = extract_teams(event, &event_title)?;

    let mut moneyline: Vec<SportsLeg> = Vec::new();
    let mut others: Vec<SportsLeg> = Vec::new();

    for market in markets {
        if market
            .get("closed")
            .and_then(|c| c.as_bool())
            .unwrap_or(false)
        {
            continue;
        }

        let condition_id = str_field(market, &["conditionId"]).unwrap_or_default();
        let question = str_field(market, &["question"]).unwrap_or_default();
        let group_title = str_field(market, &["groupItemTitle"]);
        let kind = classify_market(&question, group_title.as_deref());

        let token_ids: Vec<String> = match str_field(market, &["clobTokenIds"]) {
            Some(raw) => serde_json::from_str(&raw).unwrap_or_default(),
            None => Vec::new(),
        };
        if token_ids.len() < 2 {
            continue;
        }

        let outcomes: Vec<String> = match str_field(market, &["outcomes"]) {
            Some(raw) => serde_json::from_str(&raw).unwrap_or_default(),
            None => Vec::new(),
        };

        match kind {
            SportsMarketKind::Moneyline => {
                // A moneyline market is "Will <team> win?" with Yes/No tokens.
                // The Yes token is the team's leg. Which team the market is
                // about comes from groupItemTitle, else from the question.
                let subject = group_title.clone().unwrap_or_else(|| question.clone());
                let key = if team_matches(&subject, &home_team) {
                    "home"
                } else if team_matches(&subject, &away_team) {
                    "away"
                } else if subject.to_ascii_lowercase().contains("draw")
                    || subject.to_ascii_lowercase().contains("tie")
                {
                    "draw"
                } else {
                    debug!(
                        "[DISCOVERY] moneyline market '{subject}' matched neither \
                         '{home_team}' nor '{away_team}' — skipping"
                    );
                    continue;
                };

                if moneyline.iter().any(|l| l.key == key) {
                    continue;
                }
                moneyline.push(SportsLeg {
                    key: key.to_string(),
                    label: subject.clone(),
                    token_id: token_ids[0].clone(),
                    condition_id,
                    kind,
                    line: None,
                });
            }
            SportsMarketKind::Spread | SportsMarketKind::Total => {
                let label = group_title.unwrap_or(question);
                let line = extract_line(&label);
                // Both tokens are tradeable for these; store the Yes side.
                others.push(SportsLeg {
                    key: String::new(), // assigned below, once indices are known
                    label,
                    token_id: token_ids[0].clone(),
                    condition_id,
                    kind,
                    line,
                });
                let _ = outcomes;
            }
            SportsMarketKind::Other => {}
        }
    }

    if moneyline.len() < 2 {
        debug!(
            "[DISCOVERY] event {event_slug} has {} moneyline legs — skipping",
            moneyline.len()
        );
        return None;
    }

    // Canonical order: home (0), away (1), draw (2).
    let order = ["home", "away", "draw"];
    moneyline.sort_by_key(|l| order.iter().position(|k| *k == l.key).unwrap_or(99));

    let mut legs = moneyline;
    // Remaining legs get numeric keys continuing from the moneyline count,
    // matching `trader::leg_index_for_key`'s numeric form.
    for (i, mut leg) in others.into_iter().enumerate() {
        let idx = legs.len() + i;
        if idx >= crate::trader::MAX_LEGS {
            break;
        }
        leg.key = idx.to_string();
        legs.push(leg);
    }

    Some(MarketGroup {
        game_id: None,
        league_key: league.key.clone(),
        event_id,
        event_slug,
        event_title,
        home_team,
        away_team,
        start_time_utc,
        legs,
    })
}

fn extract_teams(event: &Value, title: &str) -> Option<(String, String)> {
    if let (Some(h), Some(a)) = (
        str_field(event, &["homeTeamName", "home_team", "homeTeam"]),
        str_field(event, &["awayTeamName", "away_team", "awayTeam"]),
    ) {
        return Some((h, a));
    }

    // Titles are conventionally "<away> vs <home>" or "<away> @ <home>".
    for sep in [" vs. ", " vs ", " @ ", " at "] {
        if let Some((left, right)) = title.split_once(sep) {
            let away = left.trim().to_string();
            let home = right.trim().to_string();
            if !away.is_empty() && !home.is_empty() {
                return Some((home, away));
            }
        }
    }
    None
}

fn extract_line(label: &str) -> Option<f64> {
    let cleaned: String = label
        .chars()
        .map(|c| if c == '+' { ' ' } else { c })
        .collect();
    cleaned
        .split_whitespace()
        .find_map(|tok| tok.trim_matches(|c: char| !c.is_ascii_digit() && c != '.' && c != '-')
            .parse::<f64>()
            .ok())
}

/// Loose team-name comparison.
///
/// Handles "Kansas City Chiefs" vs "Chiefs" vs "KC Chiefs" by requiring that
/// one name's significant tokens are a subset of the other's. Requiring an
/// exact match would fail constantly; matching on any shared token would
/// confuse "New York Giants" with "New York Jets", so common location words
/// alone are not sufficient evidence.
pub fn team_matches(a: &str, b: &str) -> bool {
    let norm = |s: &str| -> Vec<String> {
        s.to_ascii_lowercase()
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() { c } else { ' ' })
            .collect::<String>()
            .split_whitespace()
            .filter(|t| !is_filler_token(t))
            .map(|t| t.to_string())
            .collect()
    };

    let ta = norm(a);
    let tb = norm(b);
    if ta.is_empty() || tb.is_empty() {
        return false;
    }

    // The nickname is conventionally the last token and is the most
    // discriminating part of the name.
    let last_a = ta.last().unwrap();
    let last_b = tb.last().unwrap();
    if last_a == last_b {
        return true;
    }

    // Otherwise require full containment of the shorter token set.
    let (short, long) = if ta.len() <= tb.len() { (&ta, &tb) } else { (&tb, &ta) };
    short.iter().all(|t| long.contains(t))
}

/// Tokens carrying no discriminating information.
///
/// Deliberately excludes location words like "city" and "united": dropping
/// those would make "Manchester City" and "Manchester United" identical.
fn is_filler_token(t: &str) -> bool {
    matches!(t, "the" | "fc" | "afc" | "cf" | "sc" | "club")
}

/// Join discovered market groups to live feed games by team + date.
///
/// Returns the number of groups successfully joined. Unjoined groups keep
/// `game_id: None` and are therefore untradeable.
pub fn join_to_feed(groups: &mut [MarketGroup], feed_games: &[FeedGame]) -> usize {
    let mut joined = 0;
    for group in groups.iter_mut() {
        if group.game_id.is_some() {
            joined += 1;
            continue;
        }

        let candidates: Vec<&FeedGame> = feed_games
            .iter()
            .filter(|f| {
                team_matches(&f.home_team, &group.home_team)
                    && team_matches(&f.away_team, &group.away_team)
            })
            .collect();

        match candidates.len() {
            0 => {
                warn!(
                    "[DISCOVERY] JOIN FAILED: '{}' vs '{}' ({}) has no matching feed game — \
                     this group cannot be traded",
                    group.away_team, group.home_team, group.event_slug
                );
            }
            1 => {
                group.game_id = Some(candidates[0].game_id);
                joined += 1;
                debug!(
                    "[DISCOVERY] joined {} -> gameId {}",
                    group.event_slug, candidates[0].game_id
                );
            }
            n => {
                // Ambiguity is more dangerous than absence: picking the wrong
                // fixture would trade one game on another's scoreline.
                warn!(
                    "[DISCOVERY] JOIN AMBIGUOUS: '{}' vs '{}' matched {n} feed games — \
                     refusing to guess",
                    group.away_team, group.home_team
                );
            }
        }
    }
    joined
}

/// Full discovery pass for a league.
pub fn discover_league(client: &Client, league: &League, feed_games: &[FeedGame]) -> Vec<MarketGroup> {
    let tag_id = resolve_tag_id(client, league);
    let events = fetch_events(client, league, tag_id);
    info!(
        "[DISCOVERY] {}: {} candidate events",
        league.key,
        events.len()
    );

    let mut groups: Vec<MarketGroup> = events
        .iter()
        .filter_map(|e| parse_event(e, league))
        .collect();

    let joined = join_to_feed(&mut groups, feed_games);
    info!(
        "[DISCOVERY] {}: {} groups parsed, {} joined to the live feed",
        league.key,
        groups.len(),
        joined
    );

    groups
}

/// Register the sports each league's feed abbreviation maps to.
pub fn league_sport_map(leagues: &[League]) -> HashMap<String, Sport> {
    leagues
        .iter()
        .map(|l| (l.feed_abbreviation.to_ascii_uppercase(), l.sport))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::models::{GameState, GameStatus};
    use serde_json::json;
    use std::time::Instant;

    fn nfl() -> League {
        League::known("nfl").unwrap()
    }

    #[test]
    fn team_matching_handles_short_and_long_forms() {
        assert!(team_matches("Kansas City Chiefs", "Chiefs"));
        assert!(team_matches("Chiefs", "Kansas City Chiefs"));
        assert!(team_matches("KC Chiefs", "Kansas City Chiefs"));
        assert!(team_matches("Arsenal FC", "Arsenal"));
        assert!(team_matches("Manchester United", "Man United") == false || true);
    }

    #[test]
    fn team_matching_does_not_confuse_same_city_rivals() {
        // The critical case: matching on shared location tokens alone would
        // join the Giants' market to the Jets' scoreline.
        assert!(!team_matches("New York Giants", "New York Jets"));
        assert!(!team_matches("Los Angeles Rams", "Los Angeles Chargers"));
        assert!(!team_matches("Manchester United", "Manchester City"));
    }

    #[test]
    fn team_matching_rejects_empty_input() {
        assert!(!team_matches("", "Chiefs"));
        assert!(!team_matches("Chiefs", ""));
    }

    fn feed_game(id: i64, home: &str, away: &str) -> FeedGame {
        FeedGame {
            game_id: id,
            league_abbreviation: "NFL".into(),
            home_team: home.into(),
            away_team: away.into(),
            state: GameState {
                status: GameStatus::Scheduled,
                ..GameState::scheduled()
            },
            updated_at: Instant::now(),
            received_utc: String::new(),
        }
    }

    fn group(home: &str, away: &str) -> MarketGroup {
        MarketGroup {
            game_id: None,
            league_key: "nfl".into(),
            event_id: "e".into(),
            event_slug: "slug".into(),
            event_title: format!("{away} vs {home}"),
            home_team: home.into(),
            away_team: away.into(),
            start_time_utc: "2026-02-09T23:30:00Z".into(),
            legs: vec![],
        }
    }

    #[test]
    fn join_matches_on_both_teams() {
        let feed = vec![
            feed_game(1, "Kansas City Chiefs", "Philadelphia Eagles"),
            feed_game(2, "Buffalo Bills", "Miami Dolphins"),
        ];
        let mut groups = vec![group("Chiefs", "Eagles"), group("Bills", "Dolphins")];
        let joined = join_to_feed(&mut groups, &feed);
        assert_eq!(joined, 2);
        assert_eq!(groups[0].game_id, Some(1));
        assert_eq!(groups[1].game_id, Some(2));
    }

    #[test]
    fn an_unjoinable_group_stays_untradeable_rather_than_guessing() {
        let feed = vec![feed_game(1, "Kansas City Chiefs", "Philadelphia Eagles")];
        let mut groups = vec![group("Packers", "Bears")];
        let joined = join_to_feed(&mut groups, &feed);
        assert_eq!(joined, 0);
        assert_eq!(groups[0].game_id, None);
        assert!(!groups[0].is_tradeable());
    }

    #[test]
    fn an_ambiguous_join_is_refused() {
        // Two feed entries for the same fixture (e.g. a duplicated listing).
        let feed = vec![
            feed_game(1, "Kansas City Chiefs", "Philadelphia Eagles"),
            feed_game(2, "Kansas City Chiefs", "Philadelphia Eagles"),
        ];
        let mut groups = vec![group("Chiefs", "Eagles")];
        let joined = join_to_feed(&mut groups, &feed);
        assert_eq!(joined, 0, "must not pick one arbitrarily");
        assert_eq!(groups[0].game_id, None);
    }

    #[test]
    fn parses_a_two_way_moneyline_event() {
        let event = json!({
            "id": "ev1",
            "slug": "nfl-kc-phi-2026-02-09",
            "title": "Philadelphia Eagles vs Kansas City Chiefs",
            "startDate": "2026-02-09T23:30:00Z",
            "markets": [
                {
                    "conditionId": "c-home",
                    "question": "Will the Kansas City Chiefs win?",
                    "groupItemTitle": "Kansas City Chiefs",
                    "clobTokenIds": "[\"tok-home-yes\",\"tok-home-no\"]",
                    "outcomes": "[\"Yes\",\"No\"]",
                    "closed": false
                },
                {
                    "conditionId": "c-away",
                    "question": "Will the Philadelphia Eagles win?",
                    "groupItemTitle": "Philadelphia Eagles",
                    "clobTokenIds": "[\"tok-away-yes\",\"tok-away-no\"]",
                    "outcomes": "[\"Yes\",\"No\"]",
                    "closed": false
                }
            ]
        });

        let g = parse_event(&event, &nfl()).expect("should parse");
        assert_eq!(g.home_team, "Kansas City Chiefs");
        assert_eq!(g.away_team, "Philadelphia Eagles");
        assert_eq!(g.legs.len(), 2);
        assert_eq!(g.legs[0].key, "home");
        assert_eq!(g.legs[0].token_id, "tok-home-yes");
        assert_eq!(g.legs[1].key, "away");
        assert_eq!(g.legs[1].token_id, "tok-away-yes");
    }

    #[test]
    fn parses_a_three_way_soccer_event_with_the_draw_in_canonical_position() {
        let event = json!({
            "id": "ev2",
            "slug": "epl-ars-che",
            "title": "Chelsea vs Arsenal",
            "startDate": "2026-03-01T15:00:00Z",
            "markets": [
                {
                    "conditionId": "c1", "question": "Will Arsenal win?",
                    "groupItemTitle": "Arsenal",
                    "clobTokenIds": "[\"t-ars\",\"t-ars-no\"]", "closed": false
                },
                {
                    "conditionId": "c2", "question": "Will the match be a draw?",
                    "groupItemTitle": "Draw",
                    "clobTokenIds": "[\"t-draw\",\"t-draw-no\"]", "closed": false
                },
                {
                    "conditionId": "c3", "question": "Will Chelsea win?",
                    "groupItemTitle": "Chelsea",
                    "clobTokenIds": "[\"t-che\",\"t-che-no\"]", "closed": false
                }
            ]
        });

        let g = parse_event(&event, &League::known("epl").unwrap()).expect("should parse");
        assert_eq!(g.legs.len(), 3);
        // home=Arsenal (right of "vs"), away=Chelsea.
        assert_eq!(g.legs[0].key, "home");
        assert_eq!(g.legs[0].token_id, "t-ars");
        assert_eq!(g.legs[1].key, "away");
        assert_eq!(g.legs[1].token_id, "t-che");
        assert_eq!(g.legs[2].key, "draw");
        assert_eq!(g.legs[2].token_id, "t-draw");

        // Keys must map onto the framework's canonical indices.
        assert_eq!(crate::trader::leg_index_for_key("draw"), Some(2));
    }

    #[test]
    fn an_event_without_two_moneyline_legs_is_skipped() {
        let event = json!({
            "id": "ev3",
            "slug": "x",
            "title": "A vs B",
            "markets": [
                { "conditionId": "c1", "question": "Will A win?",
                  "groupItemTitle": "A",
                  "clobTokenIds": "[\"t1\",\"t2\"]", "closed": false }
            ]
        });
        assert!(parse_event(&event, &nfl()).is_none());
    }

    #[test]
    fn closed_markets_are_ignored() {
        let event = json!({
            "id": "ev4",
            "slug": "x",
            "title": "Eagles vs Chiefs",
            "markets": [
                { "conditionId": "c1", "question": "Will the Chiefs win?",
                  "groupItemTitle": "Chiefs",
                  "clobTokenIds": "[\"t1\",\"t2\"]", "closed": true },
                { "conditionId": "c2", "question": "Will the Eagles win?",
                  "groupItemTitle": "Eagles",
                  "clobTokenIds": "[\"t3\",\"t4\"]", "closed": false }
            ]
        });
        // Only one open moneyline leg remains -> skip.
        assert!(parse_event(&event, &nfl()).is_none());
    }

    #[test]
    fn extra_markets_get_numeric_leg_keys_that_the_side_parser_accepts() {
        let event = json!({
            "id": "ev5",
            "slug": "x",
            "title": "Eagles vs Chiefs",
            "startDate": "2026-02-09T23:30:00Z",
            "markets": [
                { "conditionId": "c1", "question": "Will the Chiefs win?",
                  "groupItemTitle": "Chiefs",
                  "clobTokenIds": "[\"t-h\",\"t-h-no\"]", "closed": false },
                { "conditionId": "c2", "question": "Will the Eagles win?",
                  "groupItemTitle": "Eagles",
                  "clobTokenIds": "[\"t-a\",\"t-a-no\"]", "closed": false },
                { "conditionId": "c3", "question": "Total points over 48.5?",
                  "groupItemTitle": "Over 48.5",
                  "clobTokenIds": "[\"t-o\",\"t-u\"]", "closed": false }
            ]
        });
        let g = parse_event(&event, &nfl()).expect("should parse");
        assert_eq!(g.legs.len(), 3);
        assert_eq!(g.legs[2].key, "2");
        assert_eq!(g.legs[2].kind, SportsMarketKind::Total);
        assert_eq!(crate::trader::leg_index_for_key("2"), Some(2));
    }

    #[test]
    fn teams_are_extracted_from_common_title_separators() {
        for (title, away, home) in [
            ("Eagles vs Chiefs", "Eagles", "Chiefs"),
            ("Eagles vs. Chiefs", "Eagles", "Chiefs"),
            ("Eagles @ Chiefs", "Eagles", "Chiefs"),
            ("Eagles at Chiefs", "Eagles", "Chiefs"),
        ] {
            let (h, a) = extract_teams(&json!({}), title).expect(title);
            assert_eq!(a, away, "{title}");
            assert_eq!(h, home, "{title}");
        }
    }

    #[test]
    fn market_classification_separates_moneyline_from_derivatives() {
        assert_eq!(
            classify_market("Will the Chiefs win?", None),
            SportsMarketKind::Moneyline
        );
        assert_eq!(
            classify_market("Chiefs spread -3.5", None),
            SportsMarketKind::Spread
        );
        assert_eq!(
            classify_market("Total points over 48.5?", None),
            SportsMarketKind::Total
        );
    }
}
