//! Game selection: which games to collect and trade.
//!
//! Everything here is driven by **CLI flags only** — there is deliberately no
//! persisted config. Changing what a bot trades means killing it and restarting
//! with different flags, which keeps the running configuration exactly equal to
//! the command line that produced it.
//!
//! Two separable concerns:
//!
//! 1. **Filtering** (`GameFilter`) — hard eligibility. A game either matches the
//!    flags or it does not, and when it does not the reason is recorded so
//!    "why didn't it bet on the Chiefs game" is answerable from the logs.
//! 2. **Ranking** (`SelectionPolicy`) — what to do when more games are eligible
//!    than the concurrency cap allows. Before this existed the worker iterated a
//!    `HashMap`, so the cap was filled first-come *in randomised order* — a
//!    different arbitrary subset on every restart, discarding the best
//!    opportunities silently.

use chrono::{DateTime, NaiveTime, Utc};
use serde::Serialize;

use super::models::{MarketGroup, SportsMarketKind};

// ---------------------------------------------------------------------------
// Filtering
// ---------------------------------------------------------------------------

/// Why a discovered game was not selected.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SkipReason {
    /// No feed `gameId` — there is no live state behind it.
    NotJoinedToFeed,
    /// Fewer than two moneyline legs.
    NoMoneyline,
    /// `--team` was given and neither side matched.
    TeamNotIncluded,
    /// Matched `--exclude-team`.
    TeamExcluded,
    /// `--game` was given and this was not one of them.
    GameNotListed,
    /// Kicks off before `--min-start`.
    StartsTooEarly,
    /// Kicks off after `--max-start`.
    StartsTooLate,
    /// None of the `--market-kind` markets are present.
    NoRequestedMarketKind,
    /// Start time could not be parsed, so time filters cannot be applied.
    UnparseableStartTime,
}

impl SkipReason {
    pub fn as_str(&self) -> &'static str {
        match self {
            SkipReason::NotJoinedToFeed => "not_joined_to_feed",
            SkipReason::NoMoneyline => "no_moneyline",
            SkipReason::TeamNotIncluded => "team_not_included",
            SkipReason::TeamExcluded => "team_excluded",
            SkipReason::GameNotListed => "game_not_listed",
            SkipReason::StartsTooEarly => "starts_too_early",
            SkipReason::StartsTooLate => "starts_too_late",
            SkipReason::NoRequestedMarketKind => "no_requested_market_kind",
            SkipReason::UnparseableStartTime => "unparseable_start_time",
        }
    }
}

/// Eligibility filter built entirely from command-line flags.
#[derive(Debug, Clone, Default)]
pub struct GameFilter {
    /// `--team`: only games involving one of these. Empty means all.
    pub teams: Vec<String>,
    /// `--exclude-team`: never these.
    pub exclude_teams: Vec<String>,
    /// `--game`: a feed gameId, or a substring of the slug / title / team names.
    pub games: Vec<String>,
    /// `--min-start` / `--max-start`, as absolute instants.
    pub min_start: Option<DateTime<Utc>>,
    pub max_start: Option<DateTime<Utc>>,
    /// `--market-kind`: required market types. Empty means moneyline only,
    /// which is what the bundled strategies trade.
    pub market_kinds: Vec<SportsMarketKind>,
}

impl GameFilter {
    /// Whether any flag actually narrows the slate.
    pub fn is_unrestricted(&self) -> bool {
        self.teams.is_empty()
            && self.exclude_teams.is_empty()
            && self.games.is_empty()
            && self.min_start.is_none()
            && self.max_start.is_none()
            && self.market_kinds.is_empty()
    }

    /// Decide whether a game is eligible.
    pub fn evaluate(&self, group: &MarketGroup) -> Result<(), SkipReason> {
        // Structural gates first — these are not user choices, they are
        // "we cannot safely trade this".
        if group.game_id.is_none() {
            return Err(SkipReason::NotJoinedToFeed);
        }
        if group.moneyline_legs().len() < 2 {
            return Err(SkipReason::NoMoneyline);
        }

        // Explicit game list overrides team filters: naming a game means you
        // want that game.
        if !self.games.is_empty() {
            if !self.games.iter().any(|g| game_matches(group, g)) {
                return Err(SkipReason::GameNotListed);
            }
        } else {
            if !self.teams.is_empty() {
                let hit = self.teams.iter().any(|t| {
                    team_matches_loose(&group.home_team, t)
                        || team_matches_loose(&group.away_team, t)
                });
                if !hit {
                    return Err(SkipReason::TeamNotIncluded);
                }
            }
            if self.exclude_teams.iter().any(|t| {
                team_matches_loose(&group.home_team, t)
                    || team_matches_loose(&group.away_team, t)
            }) {
                return Err(SkipReason::TeamExcluded);
            }
        }

        if self.min_start.is_some() || self.max_start.is_some() {
            let start = match DateTime::parse_from_rfc3339(&group.start_time_utc) {
                Ok(d) => d.with_timezone(&Utc),
                Err(_) => return Err(SkipReason::UnparseableStartTime),
            };
            if let Some(min) = self.min_start {
                if start < min {
                    return Err(SkipReason::StartsTooEarly);
                }
            }
            if let Some(max) = self.max_start {
                if start > max {
                    return Err(SkipReason::StartsTooLate);
                }
            }
        }

        if !self.market_kinds.is_empty() {
            let has = group
                .legs
                .iter()
                .any(|l| self.market_kinds.contains(&l.kind));
            if !has {
                return Err(SkipReason::NoRequestedMarketKind);
            }
        }

        Ok(())
    }

    /// Partition a discovered slate into selected games and skip records.
    pub fn apply<'a>(
        &self,
        groups: &'a [MarketGroup],
    ) -> (Vec<&'a MarketGroup>, Vec<(&'a MarketGroup, SkipReason)>) {
        let mut kept = Vec::new();
        let mut skipped = Vec::new();
        for g in groups {
            match self.evaluate(g) {
                Ok(()) => kept.push(g),
                Err(r) => skipped.push((g, r)),
            }
        }
        (kept, skipped)
    }
}

/// Does `needle` identify this game?
///
/// A bare number matches the feed `gameId` exactly. Anything else is a
/// case-insensitive substring match against the slug, the event title, the
/// directory key, or `"away vs home"` — so `--game kc-phi`, `--game 5127839`
/// and `--game "eagles vs chiefs"` all work.
pub fn game_matches(group: &MarketGroup, needle: &str) -> bool {
    let n = needle.trim().to_ascii_lowercase();
    if n.is_empty() {
        return false;
    }

    if let Ok(id) = n.parse::<i64>() {
        return group.game_id == Some(id);
    }

    let haystacks = [
        group.event_slug.to_ascii_lowercase(),
        group.event_title.to_ascii_lowercase(),
        group.dir_name().to_ascii_lowercase(),
        format!(
            "{} vs {}",
            group.away_team.to_ascii_lowercase(),
            group.home_team.to_ascii_lowercase()
        ),
    ];
    haystacks.iter().any(|h| h.contains(&n))
}

/// Loose team match for flag input.
///
/// Deliberately more permissive than the discovery join: here a false positive
/// only means collecting a game you did not want, whereas in the join it would
/// mean trading one game on another's scoreline.
pub fn team_matches_loose(team: &str, needle: &str) -> bool {
    let t = team.to_ascii_lowercase();
    let n = needle.trim().to_ascii_lowercase();
    !n.is_empty() && (t.contains(&n) || n.contains(&t))
}

/// Parse `--min-start` / `--max-start`.
///
/// Accepts a full RFC3339 instant, or a bare `HH:MM` which is interpreted as
/// that time **today, UTC** — convenient for "only the evening slate".
pub fn parse_start_bound(raw: &str, now: DateTime<Utc>) -> Option<DateTime<Utc>> {
    let s = raw.trim();
    if let Ok(dt) = DateTime::parse_from_rfc3339(s) {
        return Some(dt.with_timezone(&Utc));
    }
    let t = NaiveTime::parse_from_str(s, "%H:%M")
        .or_else(|_| NaiveTime::parse_from_str(s, "%H:%M:%S"))
        .ok()?;
    Some(now.date_naive().and_time(t).and_utc())
}

/// Parse a `--market-kind` value.
pub fn parse_market_kind(raw: &str) -> Option<SportsMarketKind> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "moneyline" | "ml" | "winner" => Some(SportsMarketKind::Moneyline),
        "spread" | "handicap" => Some(SportsMarketKind::Spread),
        "total" | "totals" | "ou" | "overunder" => Some(SportsMarketKind::Total),
        _ => None,
    }
}

/// Build a filter from raw CLI strings, reporting bad input rather than
/// silently ignoring it — a typo'd `--market-kind totls` that quietly matched
/// everything would be worse than an error.
pub fn filter_from_flags(
    teams: &[String],
    exclude_teams: &[String],
    games: &[String],
    min_start: Option<&str>,
    max_start: Option<&str>,
    market_kinds: &[String],
    now: DateTime<Utc>,
) -> Result<GameFilter, String> {
    let mut kinds = Vec::new();
    for raw in market_kinds {
        match parse_market_kind(raw) {
            Some(k) => kinds.push(k),
            None => {
                return Err(format!(
                    "unknown --market-kind '{raw}' (expected moneyline, spread or total)"
                ))
            }
        }
    }

    let min = match min_start {
        Some(raw) => Some(parse_start_bound(raw, now).ok_or_else(|| {
            format!("unparseable --min-start '{raw}' (expected RFC3339 or HH:MM)")
        })?),
        None => None,
    };
    let max = match max_start {
        Some(raw) => Some(parse_start_bound(raw, now).ok_or_else(|| {
            format!("unparseable --max-start '{raw}' (expected RFC3339 or HH:MM)")
        })?),
        None => None,
    };

    if let (Some(a), Some(b)) = (min, max) {
        if a > b {
            return Err(format!(
                "--min-start ({a}) is after --max-start ({b}); no game can match"
            ));
        }
    }

    Ok(GameFilter {
        teams: teams.to_vec(),
        exclude_teams: exclude_teams.to_vec(),
        games: games.to_vec(),
        min_start: min,
        max_start: max,
        market_kinds: kinds,
    })
}

/// One-line human summary of a slate, for `--list-games`.
pub fn describe(group: &MarketGroup) -> String {
    format!(
        "{:>10}  {}  {} @ {}  [{}]",
        group.game_id.map(|g| g.to_string()).unwrap_or_else(|| "-".into()),
        group.start_time_utc.get(0..16).unwrap_or(""),
        group.away_team,
        group.home_team,
        group
            .legs
            .iter()
            .map(|l| l.key.as_str())
            .collect::<Vec<_>>()
            .join(","),
    )
}

// ---------------------------------------------------------------------------
// Ranking
// ---------------------------------------------------------------------------

/// How to choose when more games are eligible than the concurrency cap allows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SelectionPolicy {
    /// Deterministic pseudo-random, seeded. Unbiased across the slate and
    /// reproducible across restarts — unlike the `HashMap` iteration this
    /// replaces, which gave a different arbitrary subset every run.
    Random,
    /// Largest absolute model-vs-market edge first.
    Edge,
    /// Earliest kickoff first.
    Scheduled,
    /// Deepest book first.
    Liquidity,
}

impl SelectionPolicy {
    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "random" => Some(SelectionPolicy::Random),
            "edge" => Some(SelectionPolicy::Edge),
            "scheduled" | "time" | "kickoff" => Some(SelectionPolicy::Scheduled),
            "liquidity" | "depth" => Some(SelectionPolicy::Liquidity),
            _ => None,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            SelectionPolicy::Random => "random",
            SelectionPolicy::Edge => "edge",
            SelectionPolicy::Scheduled => "scheduled",
            SelectionPolicy::Liquidity => "liquidity",
        }
    }
}

/// A game competing for a concurrency slot.
#[derive(Debug, Clone, PartialEq)]
pub struct Candidate {
    pub game_id: i64,
    /// Absolute model-vs-market edge from the last tick, if any.
    pub edge: Option<f64>,
    /// Summed top-of-book depth from the last tick, if any.
    pub liquidity: Option<f64>,
    /// Kickoff, epoch seconds.
    pub start_epoch: Option<i64>,
    /// Games already holding a position always sort first — they may need to
    /// exit, and losing their slot would strand the position.
    pub already_open: bool,
}

/// splitmix64 — a good avalanche in a few lines, so ordering does not clump.
fn mix(mut x: u64) -> u64 {
    x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = x;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// Order candidates best-first for the given policy.
///
/// The result depends only on the candidate *set*, never on the order they were
/// supplied in — that is what makes it stable across restarts regardless of
/// hash-map iteration order.
pub fn rank(candidates: &mut [Candidate], policy: SelectionPolicy, seed: u64) {
    candidates.sort_by(|a, b| {
        // Open positions always outrank contenders.
        b.already_open
            .cmp(&a.already_open)
            .then_with(|| match policy {
                SelectionPolicy::Random => {
                    mix(seed ^ (a.game_id as u64)).cmp(&mix(seed ^ (b.game_id as u64)))
                }
                SelectionPolicy::Edge => cmp_desc_opt(a.edge, b.edge),
                SelectionPolicy::Liquidity => cmp_desc_opt(a.liquidity, b.liquidity),
                SelectionPolicy::Scheduled => match (a.start_epoch, b.start_epoch) {
                    (Some(x), Some(y)) => x.cmp(&y),
                    (Some(_), None) => std::cmp::Ordering::Less,
                    (None, Some(_)) => std::cmp::Ordering::Greater,
                    (None, None) => std::cmp::Ordering::Equal,
                },
            })
            // Final tiebreak on id so the order is total and reproducible.
            .then_with(|| a.game_id.cmp(&b.game_id))
    });
}

/// Descending by value; `None` sorts last.
fn cmp_desc_opt(a: Option<f64>, b: Option<f64>) -> std::cmp::Ordering {
    match (a, b) {
        (Some(x), Some(y)) => y.partial_cmp(&x).unwrap_or(std::cmp::Ordering::Equal),
        (Some(_), None) => std::cmp::Ordering::Less,
        (None, Some(_)) => std::cmp::Ordering::Greater,
        (None, None) => std::cmp::Ordering::Equal,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::models::SportsLeg;

    fn leg(key: &str, kind: SportsMarketKind) -> SportsLeg {
        SportsLeg {
            key: key.into(),
            label: key.into(),
            token_id: format!("t-{key}"),
            condition_id: "c".into(),
            kind,
            line: None,
        }
    }

    fn group(id: Option<i64>, home: &str, away: &str, start: &str) -> MarketGroup {
        MarketGroup {
            game_id: id,
            league_key: "nfl".into(),
            event_id: "e".into(),
            event_slug: format!(
                "nfl-{}-{}-2026-02-09",
                away.to_ascii_lowercase().replace(' ', "-"),
                home.to_ascii_lowercase().replace(' ', "-")
            ),
            event_title: format!("{away} vs {home}"),
            home_team: home.into(),
            away_team: away.into(),
            start_time_utc: start.into(),
            legs: vec![
                leg("home", SportsMarketKind::Moneyline),
                leg("away", SportsMarketKind::Moneyline),
            ],
        }
    }

    fn chiefs() -> MarketGroup {
        group(
            Some(1),
            "Kansas City Chiefs",
            "Philadelphia Eagles",
            "2026-02-09T23:30:00Z",
        )
    }

    fn bills() -> MarketGroup {
        group(
            Some(2),
            "Buffalo Bills",
            "Miami Dolphins",
            "2026-02-09T18:00:00Z",
        )
    }

    // ---- filtering ----

    #[test]
    fn an_unrestricted_filter_accepts_a_normal_game() {
        let f = GameFilter::default();
        assert!(f.is_unrestricted());
        assert_eq!(f.evaluate(&chiefs()), Ok(()));
    }

    #[test]
    fn structural_gates_apply_even_with_no_flags() {
        let f = GameFilter::default();

        let mut unjoined = chiefs();
        unjoined.game_id = None;
        assert_eq!(f.evaluate(&unjoined), Err(SkipReason::NotJoinedToFeed));

        let mut one_leg = chiefs();
        one_leg.legs.truncate(1);
        assert_eq!(f.evaluate(&one_leg), Err(SkipReason::NoMoneyline));
    }

    #[test]
    fn team_include_matches_either_side() {
        let f = GameFilter {
            teams: vec!["Chiefs".into()],
            ..Default::default()
        };
        assert_eq!(f.evaluate(&chiefs()), Ok(()));
        assert_eq!(f.evaluate(&bills()), Err(SkipReason::TeamNotIncluded));

        // Away side too.
        let f2 = GameFilter {
            teams: vec!["Eagles".into()],
            ..Default::default()
        };
        assert_eq!(f2.evaluate(&chiefs()), Ok(()));
    }

    #[test]
    fn team_exclude_wins_over_nothing_else() {
        let f = GameFilter {
            exclude_teams: vec!["Eagles".into()],
            ..Default::default()
        };
        assert_eq!(f.evaluate(&chiefs()), Err(SkipReason::TeamExcluded));
        assert_eq!(f.evaluate(&bills()), Ok(()));
    }

    #[test]
    fn include_and_exclude_compose() {
        // Include the whole AFC-ish set but drop one fixture.
        let f = GameFilter {
            teams: vec!["Chiefs".into(), "Bills".into()],
            exclude_teams: vec!["Dolphins".into()],
            ..Default::default()
        };
        assert_eq!(f.evaluate(&chiefs()), Ok(()));
        assert_eq!(f.evaluate(&bills()), Err(SkipReason::TeamExcluded));
    }

    #[test]
    fn a_specific_game_can_be_named_by_id() {
        let f = GameFilter {
            games: vec!["1".into()],
            ..Default::default()
        };
        assert_eq!(f.evaluate(&chiefs()), Ok(()));
        assert_eq!(f.evaluate(&bills()), Err(SkipReason::GameNotListed));
    }

    #[test]
    fn a_specific_game_can_be_named_by_slug_or_title_fragment() {
        for needle in [
            "philadelphia-eagles-kansas-city-chiefs",
            "eagles vs kansas",
            "Eagles vs Kansas City Chiefs",
        ] {
            let f = GameFilter {
                games: vec![needle.into()],
                ..Default::default()
            };
            assert_eq!(f.evaluate(&chiefs()), Ok(()), "needle {needle:?}");
        }
    }

    #[test]
    fn naming_games_overrides_team_filters() {
        // You asked for this game by name; an unrelated --team must not veto it.
        let f = GameFilter {
            games: vec!["1".into()],
            teams: vec!["Packers".into()],
            exclude_teams: vec!["Chiefs".into()],
            ..Default::default()
        };
        assert_eq!(f.evaluate(&chiefs()), Ok(()));
    }

    #[test]
    fn start_time_bounds_are_applied() {
        let f = GameFilter {
            min_start: parse_start_bound("2026-02-09T20:00:00Z", Utc::now()),
            ..Default::default()
        };
        assert_eq!(f.evaluate(&chiefs()), Ok(()), "23:30 is after 20:00");
        assert_eq!(f.evaluate(&bills()), Err(SkipReason::StartsTooEarly));

        let f2 = GameFilter {
            max_start: parse_start_bound("2026-02-09T20:00:00Z", Utc::now()),
            ..Default::default()
        };
        assert_eq!(f2.evaluate(&chiefs()), Err(SkipReason::StartsTooLate));
        assert_eq!(f2.evaluate(&bills()), Ok(()));
    }

    #[test]
    fn an_unparseable_start_is_reported_not_silently_accepted() {
        let mut g = chiefs();
        g.start_time_utc = "sometime tuesday".into();
        let f = GameFilter {
            min_start: Some(Utc::now()),
            ..Default::default()
        };
        assert_eq!(f.evaluate(&g), Err(SkipReason::UnparseableStartTime));

        // With no time filter it is irrelevant and must not block.
        assert_eq!(GameFilter::default().evaluate(&g), Ok(()));
    }

    #[test]
    fn market_kind_filter_requires_the_market_to_exist() {
        let f = GameFilter {
            market_kinds: vec![SportsMarketKind::Total],
            ..Default::default()
        };
        assert_eq!(f.evaluate(&chiefs()), Err(SkipReason::NoRequestedMarketKind));

        let mut with_total = chiefs();
        with_total.legs.push(leg("2", SportsMarketKind::Total));
        assert_eq!(f.evaluate(&with_total), Ok(()));
    }

    #[test]
    fn apply_partitions_and_explains_every_rejection() {
        let f = GameFilter {
            teams: vec!["Chiefs".into()],
            ..Default::default()
        };
        let groups = vec![chiefs(), bills()];
        let (kept, skipped) = f.apply(&groups);
        assert_eq!(kept.len(), 1);
        assert_eq!(kept[0].game_id, Some(1));
        assert_eq!(skipped.len(), 1);
        assert_eq!(skipped[0].1, SkipReason::TeamNotIncluded);
    }

    #[test]
    fn start_bound_accepts_bare_clock_times_as_today_utc() {
        let now = DateTime::parse_from_rfc3339("2026-02-09T05:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let b = parse_start_bound("18:00", now).unwrap();
        assert_eq!(b.to_rfc3339(), "2026-02-09T18:00:00+00:00");
        assert!(parse_start_bound("garbage", now).is_none());
    }

    #[test]
    fn market_kind_aliases_parse() {
        assert_eq!(parse_market_kind("ml"), Some(SportsMarketKind::Moneyline));
        assert_eq!(parse_market_kind("Moneyline"), Some(SportsMarketKind::Moneyline));
        assert_eq!(parse_market_kind("spread"), Some(SportsMarketKind::Spread));
        assert_eq!(parse_market_kind("totals"), Some(SportsMarketKind::Total));
        assert_eq!(parse_market_kind("nonsense"), None);
    }

    #[test]
    fn flag_builder_rejects_bad_market_kinds() {
        let e = filter_from_flags(&[], &[], &[], None, None, &["totls".into()], Utc::now())
            .unwrap_err();
        assert!(e.contains("totls"), "{e}");
    }

    #[test]
    fn flag_builder_rejects_unparseable_times() {
        let e = filter_from_flags(&[], &[], &[], Some("half past six"), None, &[], Utc::now())
            .unwrap_err();
        assert!(e.contains("min-start"), "{e}");
    }

    #[test]
    fn flag_builder_rejects_an_impossible_time_window() {
        let e = filter_from_flags(
            &[],
            &[],
            &[],
            Some("2026-02-09T22:00:00Z"),
            Some("2026-02-09T18:00:00Z"),
            &[],
            Utc::now(),
        )
        .unwrap_err();
        assert!(e.contains("no game can match"), "{e}");
    }

    #[test]
    fn flag_builder_accepts_a_realistic_combination() {
        let f = filter_from_flags(
            &["Chiefs".into()],
            &["Jaguars".into()],
            &[],
            Some("18:00"),
            None,
            &["moneyline".into(), "total".into()],
            Utc::now(),
        )
        .expect("should build");
        assert_eq!(f.teams.len(), 1);
        assert_eq!(f.market_kinds.len(), 2);
        assert!(f.min_start.is_some());
        assert!(!f.is_unrestricted());
    }

    // ---- ranking ----

    fn cand(id: i64, edge: Option<f64>, liq: Option<f64>, start: Option<i64>) -> Candidate {
        Candidate {
            game_id: id,
            edge,
            liquidity: liq,
            start_epoch: start,
            already_open: false,
        }
    }

    #[test]
    fn random_ranking_is_stable_across_input_orderings() {
        // This is the bug being fixed: HashMap iteration gave a different
        // subset every restart. The result must depend only on the *set*.
        let mut a: Vec<Candidate> = (1..=8).map(|i| cand(i, None, None, None)).collect();
        let mut b: Vec<Candidate> = (1..=8).rev().map(|i| cand(i, None, None, None)).collect();

        rank(&mut a, SelectionPolicy::Random, 42);
        rank(&mut b, SelectionPolicy::Random, 42);

        let ids = |v: &Vec<Candidate>| v.iter().map(|c| c.game_id).collect::<Vec<_>>();
        assert_eq!(ids(&a), ids(&b));
    }

    #[test]
    fn random_ranking_is_reproducible_across_runs() {
        let run = || {
            let mut v: Vec<Candidate> = (1..=13).map(|i| cand(i, None, None, None)).collect();
            rank(&mut v, SelectionPolicy::Random, 7);
            v.iter().map(|c| c.game_id).collect::<Vec<_>>()
        };
        assert_eq!(run(), run());
    }

    #[test]
    fn different_seeds_give_different_orders() {
        let with = |seed| {
            let mut v: Vec<Candidate> = (1..=13).map(|i| cand(i, None, None, None)).collect();
            rank(&mut v, SelectionPolicy::Random, seed);
            v.iter().map(|c| c.game_id).collect::<Vec<_>>()
        };
        assert_ne!(with(1), with(2), "the seed must actually resample");
    }

    #[test]
    fn random_ranking_is_not_merely_sorted_by_id() {
        let mut v: Vec<Candidate> = (1..=13).map(|i| cand(i, None, None, None)).collect();
        rank(&mut v, SelectionPolicy::Random, 99);
        let ids: Vec<i64> = v.iter().map(|c| c.game_id).collect();
        assert_ne!(ids, (1..=13).collect::<Vec<_>>());
    }

    #[test]
    fn edge_ranking_puts_the_biggest_edge_first() {
        let mut v = vec![
            cand(1, Some(0.02), None, None),
            cand(2, Some(0.11), None, None),
            cand(3, Some(0.07), None, None),
        ];
        rank(&mut v, SelectionPolicy::Edge, 0);
        assert_eq!(v.iter().map(|c| c.game_id).collect::<Vec<_>>(), vec![2, 3, 1]);
    }

    #[test]
    fn games_with_no_measurement_sort_last_not_first() {
        // A game that has never ticked has no edge; it must not outrank a
        // measured opportunity.
        let mut v = vec![
            cand(1, None, None, None),
            cand(2, Some(0.03), None, None),
        ];
        rank(&mut v, SelectionPolicy::Edge, 0);
        assert_eq!(v[0].game_id, 2);
    }

    #[test]
    fn scheduled_ranking_is_earliest_first() {
        let mut v = vec![
            cand(1, None, None, Some(300)),
            cand(2, None, None, Some(100)),
            cand(3, None, None, Some(200)),
        ];
        rank(&mut v, SelectionPolicy::Scheduled, 0);
        assert_eq!(v.iter().map(|c| c.game_id).collect::<Vec<_>>(), vec![2, 3, 1]);
    }

    #[test]
    fn liquidity_ranking_is_deepest_first() {
        let mut v = vec![
            cand(1, None, Some(500.0), None),
            cand(2, None, Some(9000.0), None),
        ];
        rank(&mut v, SelectionPolicy::Liquidity, 0);
        assert_eq!(v[0].game_id, 2);
    }

    #[test]
    fn open_positions_always_outrank_contenders() {
        // Otherwise a game holding a position could lose its slot and be
        // stranded, unable to exit.
        let mut v = vec![
            cand(1, Some(0.99), None, None),
            Candidate {
                already_open: true,
                ..cand(2, Some(0.01), None, None)
            },
        ];
        rank(&mut v, SelectionPolicy::Edge, 0);
        assert_eq!(v[0].game_id, 2, "the open game must keep priority");
    }

    #[test]
    fn ranking_is_a_total_order_even_when_everything_ties() {
        let mut v: Vec<Candidate> = (1..=5)
            .map(|i| cand(i, Some(0.05), Some(1.0), Some(10)))
            .collect();
        rank(&mut v, SelectionPolicy::Edge, 0);
        // Tie broken by id, so the order is reproducible rather than arbitrary.
        assert_eq!(v.iter().map(|c| c.game_id).collect::<Vec<_>>(), vec![1, 2, 3, 4, 5]);
    }

    #[test]
    fn policy_names_parse_and_round_trip() {
        for name in ["random", "edge", "scheduled", "liquidity"] {
            let p = SelectionPolicy::parse(name).expect(name);
            assert_eq!(p.as_str(), name);
        }
        assert_eq!(SelectionPolicy::parse("kickoff"), Some(SelectionPolicy::Scheduled));
        assert_eq!(SelectionPolicy::parse("nope"), None);
    }
}
