//! Domain types for sports markets.
//!
//! The mapping onto the existing framework is deliberate: a **game is a
//! session**. `GameSample` is the sports analog of `models::Sample`, and
//! `GameState` plays the role that the BTC price plays for crypto — the
//! exogenous truth signal against which the book is judged.

use serde::{Deserialize, Serialize};

use crate::economics::MarketEconomics;
use crate::trader::LegSpec;

// ---------------------------------------------------------------------------
// Sport / league
// ---------------------------------------------------------------------------

/// Which sport a league belongs to. Determines clock semantics, the shape of
/// the fair-value model, and whether a draw is possible.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Sport {
    Soccer,
    AmericanFootball,
}

impl Sport {
    /// Regulation length in minutes. Used to convert a clock into "fraction of
    /// the game elapsed" and to compute remaining-time goal/score expectations.
    pub fn regulation_minutes(&self) -> f64 {
        match self {
            Sport::Soccer => 90.0,
            Sport::AmericanFootball => 60.0,
        }
    }

    /// Whether a tie is a distinct tradeable outcome.
    ///
    /// Soccer draws are ~25% of matches, so a two-way approximation is simply
    /// wrong. NFL ties exist but are rare enough (and usually not offered as a
    /// separate market) that the moneyline is treated as two-way.
    pub fn has_draw(&self) -> bool {
        matches!(self, Sport::Soccer)
    }
}

/// A league we collect and trade.
#[derive(Debug, Clone, PartialEq)]
pub struct League {
    /// Short key used in paths and CLI flags: `nfl`, `epl`, `laliga`.
    pub key: String,
    pub sport: Sport,
    /// Abbreviation as it appears in the Polymarket sports feed
    /// (`leagueAbbreviation`), e.g. `NFL`, `EPL`.
    pub feed_abbreviation: String,
    /// ESPN league path segment: `nfl`, `eng.1`, `esp.1`.
    pub espn_league: String,
    /// ESPN sport path segment: `football`, `soccer`.
    pub espn_sport: String,
    /// Gamma tag id used to enumerate events. `None` means it must be resolved
    /// at runtime from `GET /tags`, which is the normal path — hardcoding tag
    /// ids goes stale.
    pub gamma_tag_id: Option<u64>,
    /// Substring matched against Gamma event slugs as a fallback when the tag
    /// lookup fails.
    pub slug_hint: String,
    pub economics: MarketEconomics,
}

impl League {
    pub fn known(key: &str) -> Option<League> {
        let e = MarketEconomics::sports();
        Some(match key {
            "nfl" => League {
                key: "nfl".into(),
                sport: Sport::AmericanFootball,
                feed_abbreviation: "NFL".into(),
                espn_league: "nfl".into(),
                espn_sport: "football".into(),
                gamma_tag_id: None,
                slug_hint: "nfl".into(),
                economics: e,
            },
            "epl" => League {
                key: "epl".into(),
                sport: Sport::Soccer,
                feed_abbreviation: "EPL".into(),
                espn_league: "eng.1".into(),
                espn_sport: "soccer".into(),
                gamma_tag_id: None,
                slug_hint: "epl".into(),
                economics: e,
            },
            "laliga" => League {
                key: "laliga".into(),
                sport: Sport::Soccer,
                feed_abbreviation: "LALIGA".into(),
                espn_league: "esp.1".into(),
                espn_sport: "soccer".into(),
                gamma_tag_id: None,
                slug_hint: "laliga".into(),
                economics: e,
            },
            "seriea" => League {
                key: "seriea".into(),
                sport: Sport::Soccer,
                feed_abbreviation: "SERIEA".into(),
                espn_league: "ita.1".into(),
                espn_sport: "soccer".into(),
                gamma_tag_id: None,
                slug_hint: "serie-a".into(),
                economics: e,
            },
            "bundesliga" => League {
                key: "bundesliga".into(),
                sport: Sport::Soccer,
                feed_abbreviation: "BUNDESLIGA".into(),
                espn_league: "ger.1".into(),
                espn_sport: "soccer".into(),
                gamma_tag_id: None,
                slug_hint: "bundesliga".into(),
                economics: e,
            },
            "ucl" => League {
                key: "ucl".into(),
                sport: Sport::Soccer,
                feed_abbreviation: "UCL".into(),
                espn_league: "uefa.champions".into(),
                espn_sport: "soccer".into(),
                gamma_tag_id: None,
                slug_hint: "champions-league".into(),
                economics: e,
            },
            "mls" => League {
                key: "mls".into(),
                sport: Sport::Soccer,
                feed_abbreviation: "MLS".into(),
                espn_league: "usa.1".into(),
                espn_sport: "soccer".into(),
                gamma_tag_id: None,
                slug_hint: "mls".into(),
                economics: e,
            },
            _ => return None,
        })
    }

    pub fn all_keys() -> Vec<&'static str> {
        vec!["nfl", "epl", "laliga", "seriea", "bundesliga", "ucl", "mls"]
    }
}

// ---------------------------------------------------------------------------
// Game state
// ---------------------------------------------------------------------------

/// Coarse lifecycle status, normalised across feeds.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GameStatus {
    Scheduled,
    InProgress,
    Halftime,
    /// Delayed, suspended, or otherwise stopped but not finished.
    Suspended,
    Final,
    Unknown,
}

impl GameStatus {
    pub fn from_feed(raw: &str) -> GameStatus {
        let s = raw.to_ascii_lowercase().replace([' ', '_', '-'], "");
        match s.as_str() {
            "scheduled" | "pregame" | "notstarted" | "upcoming" => GameStatus::Scheduled,
            "inprogress" | "live" | "inplay" | "playing" => GameStatus::InProgress,
            "halftime" | "ht" | "break" => GameStatus::Halftime,
            "delayed" | "suspended" | "postponed" | "interrupted" => GameStatus::Suspended,
            "final" | "ended" | "finished" | "ft" | "complete" | "completed" => GameStatus::Final,
            _ => GameStatus::Unknown,
        }
    }

    pub fn is_live(&self) -> bool {
        matches!(self, GameStatus::InProgress | GameStatus::Halftime)
    }
}

/// A snapshot of the game itself, independent of any market.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GameState {
    pub home_score: i32,
    pub away_score: i32,
    /// Raw period label from the feed (`Q4`, `2H`, `HT`).
    pub period: String,
    /// Raw clock string from the feed (`05:12`).
    pub clock: String,
    pub status: GameStatus,
    /// Minutes of regulation elapsed, normalised across sports. `None` when the
    /// feed's period/clock could not be interpreted — callers must not guess.
    pub elapsed_minutes: Option<f64>,
    /// Red cards, soccer only.
    #[serde(default)]
    pub home_red_cards: i32,
    #[serde(default)]
    pub away_red_cards: i32,
    /// True when the feed indicates a play/goal is under official review.
    /// Trading is suspended while set — see `review` handling in the worker.
    #[serde(default)]
    pub under_review: bool,
}

impl GameState {
    pub fn scheduled() -> Self {
        Self {
            home_score: 0,
            away_score: 0,
            period: String::new(),
            clock: String::new(),
            status: GameStatus::Scheduled,
            elapsed_minutes: None,
            home_red_cards: 0,
            away_red_cards: 0,
            under_review: false,
        }
    }

    pub fn score_diff(&self) -> i32 {
        self.home_score - self.away_score
    }

    /// Minutes left in regulation, clamped to >= 0.
    pub fn remaining_minutes(&self, sport: Sport) -> Option<f64> {
        self.elapsed_minutes
            .map(|e| (sport.regulation_minutes() - e).max(0.0))
    }

    /// Fraction of regulation elapsed, in [0, 1].
    pub fn elapsed_fraction(&self, sport: Sport) -> Option<f64> {
        self.elapsed_minutes
            .map(|e| (e / sport.regulation_minutes()).clamp(0.0, 1.0))
    }

    /// Whether the *scoreline or card state* differs — i.e. something happened
    /// that should move the price. Clock ticks alone do not count.
    pub fn is_material_change(&self, other: &GameState) -> bool {
        self.home_score != other.home_score
            || self.away_score != other.away_score
            || self.home_red_cards != other.home_red_cards
            || self.away_red_cards != other.away_red_cards
            || self.status != other.status
            || self.under_review != other.under_review
    }
}

/// Parse a `"98-94"` style score string into `(home, away)`.
///
/// Returns `None` rather than defaulting to 0-0: a mis-parsed score that
/// silently reads as a tie is far more dangerous than no score at all.
pub fn parse_score(raw: &str) -> Option<(i32, i32)> {
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    let (h, a) = raw.split_once('-')?;
    let home = h.trim().parse::<i32>().ok()?;
    let away = a.trim().parse::<i32>().ok()?;
    Some((home, away))
}

/// Convert a feed period + clock into minutes of regulation elapsed.
///
/// Feeds are inconsistent about whether the clock counts up or down, so this
/// is deliberately conservative: anything it cannot interpret returns `None`
/// and the caller treats elapsed time as unknown rather than guessing.
///
/// - Soccer clocks count **up** and are continuous across the match
///   (`2H 67:30` means 67.5 minutes played).
/// - American football clocks count **down** within a quarter
///   (`Q4 05:12` means 45 + (15 - 5.2) = 54.8 minutes played).
pub fn parse_elapsed_minutes(sport: Sport, period: &str, clock: &str) -> Option<f64> {
    let p = period.trim().to_ascii_uppercase();
    let clock_minutes = parse_clock_minutes(clock);

    match sport {
        Sport::Soccer => {
            if p.contains("HT") || p.contains("HALF") {
                return Some(45.0);
            }
            if p.contains("FT") || p.starts_with("FULL") {
                return Some(90.0);
            }
            // Soccer clocks are continuous, so the clock alone is the answer
            // when present.
            if let Some(m) = clock_minutes {
                return Some(m.min(120.0));
            }
            if p.starts_with("1") {
                Some(0.0)
            } else if p.starts_with("2") {
                Some(45.0)
            } else {
                None
            }
        }
        Sport::AmericanFootball => {
            let quarter = if p.contains("OT") {
                5
            } else if p.starts_with('Q') {
                p[1..].trim().parse::<i32>().ok()?
            } else if p.contains("HALF") {
                return Some(30.0);
            } else if p.contains("FINAL") {
                return Some(60.0);
            } else {
                p.trim().parse::<i32>().ok()?
            };
            if !(1..=5).contains(&quarter) {
                return None;
            }
            if quarter == 5 {
                // Overtime — treat regulation as complete.
                return Some(60.0);
            }
            let quarter_len = 15.0;
            let remaining_in_quarter = clock_minutes.unwrap_or(quarter_len);
            let elapsed_in_quarter = (quarter_len - remaining_in_quarter).clamp(0.0, quarter_len);
            Some((quarter - 1) as f64 * quarter_len + elapsed_in_quarter)
        }
    }
}

/// Parse `MM:SS` (or `M:SS`, or a bare minute count) into minutes.
fn parse_clock_minutes(clock: &str) -> Option<f64> {
    let c = clock.trim().trim_end_matches('\'');
    if c.is_empty() {
        return None;
    }
    if let Some((m, s)) = c.split_once(':') {
        let mins = m.trim().parse::<f64>().ok()?;
        let secs = s.trim().parse::<f64>().ok()?;
        return Some(mins + secs / 60.0);
    }
    c.parse::<f64>().ok()
}

// ---------------------------------------------------------------------------
// Markets
// ---------------------------------------------------------------------------

/// Kind of market within a game's event.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SportsMarketKind {
    Moneyline,
    Spread,
    Total,
    Other,
}

/// One game's tradeable markets, grouped.
///
/// A Polymarket sports event contains several binary markets (moneyline per
/// side, spread, total). They share a game but each has its own condition id
/// and order book, so they are flattened into indexed legs that the existing
/// `Position` can hold.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MarketGroup {
    /// Polymarket sports-feed game id, once joined. `None` means the join has
    /// not succeeded — such a group must not be traded, because we would have
    /// no live game state for it.
    pub game_id: Option<i64>,
    pub league_key: String,
    pub event_id: String,
    pub event_slug: String,
    pub event_title: String,
    pub home_team: String,
    pub away_team: String,
    /// Scheduled kickoff, RFC3339.
    pub start_time_utc: String,
    /// Tradeable legs in canonical index order. Index 0/1/2 are
    /// home/away/draw when a moneyline is present.
    pub legs: Vec<SportsLeg>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SportsLeg {
    /// Leg key used in side strings: `home`, `away`, `draw`, or a numeric index.
    pub key: String,
    pub label: String,
    pub token_id: String,
    pub condition_id: String,
    pub kind: SportsMarketKind,
    /// Spread/total line, when applicable.
    #[serde(default)]
    pub line: Option<f64>,
}

impl MarketGroup {
    /// Convert to the framework's `LegSpec` list.
    pub fn leg_specs(&self) -> Vec<LegSpec> {
        self.legs
            .iter()
            .map(|l| LegSpec::new(&l.key, &l.label, &l.token_id, &l.condition_id))
            .collect()
    }

    /// All token ids, for WebSocket subscription and batch book requests.
    pub fn token_ids(&self) -> Vec<String> {
        self.legs.iter().map(|l| l.token_id.clone()).collect()
    }

    pub fn moneyline_legs(&self) -> Vec<(usize, &SportsLeg)> {
        self.legs
            .iter()
            .enumerate()
            .filter(|(_, l)| l.kind == SportsMarketKind::Moneyline)
            .collect()
    }

    /// Leg index for a key, e.g. `home` -> 0.
    pub fn leg_index(&self, key: &str) -> Option<usize> {
        self.legs.iter().position(|l| l.key == key)
    }

    /// Stable directory name for this game's data.
    pub fn dir_name(&self) -> String {
        let date = self.start_time_utc.get(0..10).unwrap_or("unknown");
        let slug = slugify(&format!("{}-{}", self.away_team, self.home_team));
        match self.game_id {
            Some(id) => format!("{date}--{slug}--{id}"),
            None => format!("{date}--{slug}--nogameid"),
        }
    }

    /// Whether this group is safe to trade: it must be joined to a live game
    /// feed and have at least a two-sided moneyline.
    pub fn is_tradeable(&self) -> bool {
        self.game_id.is_some() && self.moneyline_legs().len() >= 2
    }
}

pub fn slugify(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut last_dash = true;
    for ch in s.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch.to_ascii_lowercase());
            last_dash = false;
        } else if !last_dash {
            out.push('-');
            last_dash = true;
        }
    }
    out.trim_matches('-').to_string()
}

// ---------------------------------------------------------------------------
// Samples
// ---------------------------------------------------------------------------

/// Best bid/ask/midpoint for one leg at one instant.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct LegBook {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub midpoint: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub best_bid: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub best_ask: Option<f64>,
    /// Cumulative size available at the top N levels, bid side then ask side.
    ///
    /// Depth is recorded because the crypto fill model — "filled when the
    /// midpoint crosses my price" — is only defensible on a book with
    /// best_bid=0.01/best_ask=0.99. Sports books have real depth, and a
    /// depth-aware backtest fill model cannot be built retroactively if depth
    /// was never captured.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub bid_levels: Vec<(f64, f64)>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub ask_levels: Vec<(f64, f64)>,
}

impl LegBook {
    pub fn spread(&self) -> Option<f64> {
        match (self.best_bid, self.best_ask) {
            (Some(b), Some(a)) => Some(a - b),
            _ => None,
        }
    }
}

/// One recorded observation of a game and its markets.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GameSample {
    pub timestamp_utc: String,
    /// Seconds since collection started for this game.
    pub elapsed_seconds: f64,
    pub state: GameState,
    /// Book per leg, parallel to `MarketGroup.legs`.
    pub books: Vec<LegBook>,
    /// Model fair value per leg, when a model was available.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub model_probs: Vec<f64>,
    /// Reference win probability from a secondary feed (ESPN), home side.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reference_home_prob: Option<f64>,
}

impl GameSample {
    pub fn leg_midpoint(&self, leg: usize) -> Option<f64> {
        self.books.get(leg).and_then(|b| b.midpoint)
    }

    /// Sum of moneyline midpoints. On a coherent two/three-way book this should
    /// be close to 1.0; a large deviation is either a stale leg or a genuine
    /// cross-leg mispricing.
    pub fn moneyline_sum(&self, group: &MarketGroup) -> Option<f64> {
        let legs = group.moneyline_legs();
        if legs.is_empty() {
            return None;
        }
        let mut sum = 0.0;
        for (i, _) in legs {
            sum += self.leg_midpoint(i)?;
        }
        Some(sum)
    }
}

/// Everything persisted for one game.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GameSessionMeta {
    pub group: MarketGroup,
    pub league_key: String,
    pub sport: Sport,
    pub collection_started_utc: String,
    #[serde(default)]
    pub collection_ended_utc: String,
    /// Final score once known.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_state: Option<GameState>,
    /// Winning leg key (`home` / `away` / `draw`) once resolved.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outcome_leg_key: Option<String>,
}

impl GameSessionMeta {
    /// Winning leg index, derived from the final score.
    pub fn outcome_leg(&self) -> Option<usize> {
        let key = self.outcome_leg_key.as_deref()?;
        self.group.leg_index(key)
    }
}

/// Determine the winning moneyline leg key from a final state.
pub fn outcome_key_from_state(sport: Sport, state: &GameState) -> Option<String> {
    if state.status != GameStatus::Final {
        return None;
    }
    Some(match state.score_diff() {
        d if d > 0 => "home".to_string(),
        d if d < 0 => "away".to_string(),
        _ if sport.has_draw() => "draw".to_string(),
        // A tie in a sport without a draw market is not resolvable to a
        // moneyline leg — refuse rather than picking a side.
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_score_handles_normal_and_rejects_garbage() {
        assert_eq!(parse_score("98-94"), Some((98, 94)));
        assert_eq!(parse_score(" 3 - 1 "), Some((3, 1)));
        assert_eq!(parse_score("0-0"), Some((0, 0)));

        // Must be None, never a silent 0-0.
        assert_eq!(parse_score(""), None);
        assert_eq!(parse_score("TBD"), None);
        assert_eq!(parse_score("98"), None);
        assert_eq!(parse_score("a-b"), None);
    }

    #[test]
    fn soccer_elapsed_follows_the_continuous_clock() {
        assert_eq!(
            parse_elapsed_minutes(Sport::Soccer, "2H", "67:30"),
            Some(67.5)
        );
        assert_eq!(parse_elapsed_minutes(Sport::Soccer, "1H", "12:00"), Some(12.0));
        assert_eq!(parse_elapsed_minutes(Sport::Soccer, "HT", ""), Some(45.0));
        assert_eq!(parse_elapsed_minutes(Sport::Soccer, "FT", ""), Some(90.0));
    }

    #[test]
    fn american_football_clock_counts_down_within_a_quarter() {
        // Q4 with 5:12 left = 45 + (15 - 5.2) = 54.8 minutes played.
        let e = parse_elapsed_minutes(Sport::AmericanFootball, "Q4", "05:12").unwrap();
        assert!((e - 54.8).abs() < 0.01, "got {e}");

        // Start of the game.
        let e = parse_elapsed_minutes(Sport::AmericanFootball, "Q1", "15:00").unwrap();
        assert!((e - 0.0).abs() < 1e-9, "got {e}");

        // End of Q2.
        let e = parse_elapsed_minutes(Sport::AmericanFootball, "Q2", "00:00").unwrap();
        assert!((e - 30.0).abs() < 1e-9, "got {e}");

        assert_eq!(
            parse_elapsed_minutes(Sport::AmericanFootball, "OT", "10:00"),
            Some(60.0)
        );
    }

    #[test]
    fn unparseable_clock_yields_none_rather_than_a_guess() {
        assert_eq!(parse_elapsed_minutes(Sport::AmericanFootball, "???", ""), None);
        assert_eq!(parse_elapsed_minutes(Sport::AmericanFootball, "Q9", "1:00"), None);
        assert_eq!(parse_elapsed_minutes(Sport::Soccer, "", ""), None);
    }

    #[test]
    fn status_normalises_across_feed_spellings() {
        assert_eq!(GameStatus::from_feed("InProgress"), GameStatus::InProgress);
        assert_eq!(GameStatus::from_feed("in_progress"), GameStatus::InProgress);
        assert_eq!(GameStatus::from_feed("LIVE"), GameStatus::InProgress);
        assert_eq!(GameStatus::from_feed("Final"), GameStatus::Final);
        assert_eq!(GameStatus::from_feed("FT"), GameStatus::Final);
        assert_eq!(GameStatus::from_feed("Halftime"), GameStatus::Halftime);
        assert_eq!(GameStatus::from_feed("Scheduled"), GameStatus::Scheduled);
        assert_eq!(GameStatus::from_feed("weird"), GameStatus::Unknown);
    }

    #[test]
    fn material_change_ignores_the_clock_but_catches_scores_and_cards() {
        let a = GameState {
            home_score: 1,
            away_score: 0,
            period: "2H".into(),
            clock: "60:00".into(),
            status: GameStatus::InProgress,
            elapsed_minutes: Some(60.0),
            home_red_cards: 0,
            away_red_cards: 0,
            under_review: false,
        };

        // Clock advanced only.
        let mut b = a.clone();
        b.clock = "61:00".into();
        b.elapsed_minutes = Some(61.0);
        assert!(!a.is_material_change(&b));

        // Goal.
        let mut c = a.clone();
        c.away_score = 1;
        assert!(a.is_material_change(&c));

        // Red card.
        let mut d = a.clone();
        d.away_red_cards = 1;
        assert!(a.is_material_change(&d));

        // Review flag.
        let mut e = a.clone();
        e.under_review = true;
        assert!(a.is_material_change(&e));
    }

    #[test]
    fn outcome_key_requires_a_final_status() {
        let mut s = GameState::scheduled();
        s.home_score = 2;
        s.away_score = 1;
        s.status = GameStatus::InProgress;
        assert_eq!(outcome_key_from_state(Sport::Soccer, &s), None);

        s.status = GameStatus::Final;
        assert_eq!(
            outcome_key_from_state(Sport::Soccer, &s).as_deref(),
            Some("home")
        );
    }

    #[test]
    fn a_tie_resolves_to_draw_only_where_a_draw_market_exists() {
        let mut s = GameState::scheduled();
        s.status = GameStatus::Final;
        s.home_score = 1;
        s.away_score = 1;

        assert_eq!(
            outcome_key_from_state(Sport::Soccer, &s).as_deref(),
            Some("draw")
        );
        // An NFL tie has no leg to resolve to — must refuse, not pick a side.
        assert_eq!(outcome_key_from_state(Sport::AmericanFootball, &s), None);
    }

    fn sample_group() -> MarketGroup {
        MarketGroup {
            game_id: Some(5127839),
            league_key: "nfl".into(),
            event_id: "e1".into(),
            event_slug: "nfl-kc-phi-2026-02-09".into(),
            event_title: "Chiefs vs Eagles".into(),
            home_team: "Kansas City Chiefs".into(),
            away_team: "Philadelphia Eagles".into(),
            start_time_utc: "2026-02-09T23:30:00Z".into(),
            legs: vec![
                SportsLeg {
                    key: "home".into(),
                    label: "Chiefs".into(),
                    token_id: "t-home".into(),
                    condition_id: "c-ml".into(),
                    kind: SportsMarketKind::Moneyline,
                    line: None,
                },
                SportsLeg {
                    key: "away".into(),
                    label: "Eagles".into(),
                    token_id: "t-away".into(),
                    condition_id: "c-ml".into(),
                    kind: SportsMarketKind::Moneyline,
                    line: None,
                },
            ],
        }
    }

    #[test]
    fn leg_specs_map_onto_canonical_indices() {
        let g = sample_group();
        let specs = g.leg_specs();
        assert_eq!(specs[0].key, "home");
        assert_eq!(specs[1].key, "away");
        // home/away must resolve to leg 0/1 through the framework's table.
        assert_eq!(crate::trader::leg_index_for_key(&specs[0].key), Some(0));
        assert_eq!(crate::trader::leg_index_for_key(&specs[1].key), Some(1));
    }

    #[test]
    fn dir_name_is_stable_and_filesystem_safe() {
        let g = sample_group();
        assert_eq!(
            g.dir_name(),
            "2026-02-09--philadelphia-eagles-kansas-city-chiefs--5127839"
        );
    }

    #[test]
    fn a_group_without_a_game_id_is_not_tradeable() {
        let mut g = sample_group();
        assert!(g.is_tradeable());

        // Failed join to the live feed -> no game state -> must not trade.
        g.game_id = None;
        assert!(!g.is_tradeable());
    }

    #[test]
    fn a_group_without_two_moneyline_legs_is_not_tradeable() {
        let mut g = sample_group();
        g.legs.truncate(1);
        assert!(!g.is_tradeable());
    }

    #[test]
    fn moneyline_sum_detects_an_incoherent_book() {
        let g = sample_group();
        let mk = |h: f64, a: f64| GameSample {
            timestamp_utc: "2026-02-09T23:45:00Z".into(),
            elapsed_seconds: 900.0,
            state: GameState::scheduled(),
            books: vec![
                LegBook {
                    midpoint: Some(h),
                    ..Default::default()
                },
                LegBook {
                    midpoint: Some(a),
                    ..Default::default()
                },
            ],
            model_probs: vec![],
            reference_home_prob: None,
        };

        let coherent = mk(0.62, 0.38).moneyline_sum(&g).unwrap();
        assert!((coherent - 1.0).abs() < 1e-9);

        let stale = mk(0.62, 0.55).moneyline_sum(&g).unwrap();
        assert!((stale - 1.17).abs() < 1e-9);
    }

    #[test]
    fn soccer_has_a_draw_and_football_does_not() {
        assert!(Sport::Soccer.has_draw());
        assert!(!Sport::AmericanFootball.has_draw());
        assert_eq!(Sport::Soccer.regulation_minutes(), 90.0);
        assert_eq!(Sport::AmericanFootball.regulation_minutes(), 60.0);
    }

    #[test]
    fn known_leagues_resolve() {
        for k in League::all_keys() {
            assert!(League::known(k).is_some(), "{k} should be known");
        }
        assert!(League::known("quidditch").is_none());
    }
}
