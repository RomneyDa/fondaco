//! Adaptive sampling cadence.
//!
//! Crypto polls at a constant ~18 Hz for 5 minutes: ~5,400 samples per session.
//! Applying that to a 3.5 hour NFL game gives ~227,000 samples of almost
//! entirely nothing, times up to 15 concurrent games. Sampling must follow the
//! *game*, not the clock.
//!
//! Edge in sports appears in bursts: immediately after a score, a red card, a
//! period change, or in the closing minutes. Between those moments the book
//! barely moves. So:
//!
//! | Phase          | Cadence                            | Trigger                            |
//! |----------------|------------------------------------|------------------------------------|
//! | Dormant        | none                               | >10 min before kickoff             |
//! | Pre-game       | 0.2 Hz                             | T-10min -> kickoff                 |
//! | Idle in-play   | 0.5 Hz, batched across all games    | clock running, nothing happening   |
//! | Hot            | 5-10 Hz, this game only            | score/card/period change, T-5min,  |
//! |                |                                    | or model-market divergence         |
//! | Position open  | >= 2 Hz floor                      | any open leg                       |
//!
//! This is ~100x fewer requests than constant polling while keeping full
//! resolution exactly when edge exists.

use std::time::{Duration, Instant};

use super::models::{GameState, GameStatus, Sport};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CadencePhase {
    Dormant,
    PreGame,
    IdleInPlay,
    Hot,
    Ended,
}

impl CadencePhase {
    pub fn as_str(&self) -> &'static str {
        match self {
            CadencePhase::Dormant => "dormant",
            CadencePhase::PreGame => "pregame",
            CadencePhase::IdleInPlay => "idle_inplay",
            CadencePhase::Hot => "hot",
            CadencePhase::Ended => "ended",
        }
    }
}

#[derive(Debug, Clone)]
pub struct CadenceConfig {
    /// How long before kickoff to leave Dormant.
    pub pregame_lead: Duration,
    pub pregame_interval: Duration,
    pub idle_interval: Duration,
    pub hot_interval: Duration,
    /// Minimum rate while holding a position, regardless of phase.
    pub position_interval: Duration,
    /// How long a game stays Hot after a trigger.
    pub hot_window: Duration,
    /// Minutes remaining at which the endgame is permanently Hot.
    pub endgame_minutes: f64,
    /// |model - market| divergence that promotes a game to Hot.
    pub divergence_threshold: f64,
    /// Heartbeat: persist a sample at least this often even if nothing changed.
    pub heartbeat: Duration,
}

impl Default for CadenceConfig {
    fn default() -> Self {
        Self {
            pregame_lead: Duration::from_secs(10 * 60),
            pregame_interval: Duration::from_secs(5),
            idle_interval: Duration::from_secs(2),
            hot_interval: Duration::from_millis(150),
            position_interval: Duration::from_millis(500),
            hot_window: Duration::from_secs(45),
            endgame_minutes: 5.0,
            divergence_threshold: 0.05,
            heartbeat: Duration::from_secs(10),
        }
    }
}

/// Per-game sampling state machine.
pub struct CadenceController {
    cfg: CadenceConfig,
    sport: Sport,
    phase: CadencePhase,
    hot_until: Option<Instant>,
    last_sample: Option<Instant>,
    last_persist: Option<Instant>,
    last_state: Option<GameState>,
    /// Set while the strategy holds any position on this game.
    has_position: bool,
    hot_trigger_count: u64,
}

impl CadenceController {
    pub fn new(sport: Sport, cfg: CadenceConfig) -> Self {
        Self {
            cfg,
            sport,
            phase: CadencePhase::Dormant,
            hot_until: None,
            last_sample: None,
            last_persist: None,
            last_state: None,
            has_position: false,
            hot_trigger_count: 0,
        }
    }

    pub fn phase(&self) -> CadencePhase {
        self.phase
    }

    pub fn hot_trigger_count(&self) -> u64 {
        self.hot_trigger_count
    }

    pub fn set_has_position(&mut self, has: bool) {
        self.has_position = has;
    }

    /// Force the game Hot — used when an external signal (model divergence, a
    /// secondary feed event) warrants a burst.
    pub fn trigger_hot(&mut self, now: Instant) {
        self.hot_until = Some(now + self.cfg.hot_window);
        self.hot_trigger_count += 1;
    }

    /// Update the phase from the latest game state.
    ///
    /// `seconds_to_kickoff` is negative once the scheduled start has passed.
    /// Returns `true` if this update triggered a Hot burst.
    pub fn observe(
        &mut self,
        now: Instant,
        state: Option<&GameState>,
        seconds_to_kickoff: Option<f64>,
    ) -> bool {
        let mut newly_hot = false;

        if let Some(s) = state {
            // A material change (score, card, period, review flag) is the
            // primary Hot trigger. Clock ticks alone are not.
            if let Some(prev) = &self.last_state {
                if prev.is_material_change(s) {
                    self.trigger_hot(now);
                    newly_hot = true;
                }
            }
            self.last_state = Some(s.clone());
        }

        let status = state.map(|s| s.status).unwrap_or(GameStatus::Scheduled);

        // Closing minutes are permanently Hot: that is where a stale book is
        // most expensive and where probabilities move fastest.
        if let Some(s) = state {
            if let Some(rem) = s.remaining_minutes(self.sport) {
                if status.is_live() && rem <= self.cfg.endgame_minutes {
                    self.hot_until = Some(now + self.cfg.hot_window);
                }
            }
        }

        let hot_active = self.hot_until.map(|t| now < t).unwrap_or(false);

        self.phase = match status {
            GameStatus::Final => CadencePhase::Ended,
            GameStatus::InProgress | GameStatus::Halftime | GameStatus::Suspended => {
                if hot_active {
                    CadencePhase::Hot
                } else {
                    CadencePhase::IdleInPlay
                }
            }
            GameStatus::Scheduled | GameStatus::Unknown => {
                match seconds_to_kickoff {
                    Some(s) if s <= self.cfg.pregame_lead.as_secs_f64() => CadencePhase::PreGame,
                    Some(_) => CadencePhase::Dormant,
                    // No schedule information: assume pre-game rather than
                    // dormant, so an unknown game is still observed.
                    None => CadencePhase::PreGame,
                }
            }
        };

        newly_hot
    }

    /// Promote to Hot when the model and the market disagree materially.
    pub fn observe_divergence(&mut self, now: Instant, model_prob: f64, market_mid: f64) -> bool {
        if (model_prob - market_mid).abs() >= self.cfg.divergence_threshold {
            self.trigger_hot(now);
            return true;
        }
        false
    }

    /// Target interval for the current phase.
    pub fn interval(&self) -> Option<Duration> {
        let base = match self.phase {
            CadencePhase::Dormant => None,
            CadencePhase::PreGame => Some(self.cfg.pregame_interval),
            CadencePhase::IdleInPlay => Some(self.cfg.idle_interval),
            CadencePhase::Hot => Some(self.cfg.hot_interval),
            CadencePhase::Ended => None,
        };

        // An open position imposes a floor regardless of phase — a stop-loss
        // cannot wait for the idle cadence. It does not, however, wake a
        // dormant or finished game.
        match (base, self.has_position) {
            (Some(b), true) => Some(b.min(self.cfg.position_interval)),
            (Some(b), false) => Some(b),
            (None, _) => None,
        }
    }

    /// Whether a sample is due now.
    pub fn should_sample(&self, now: Instant) -> bool {
        let interval = match self.interval() {
            Some(i) => i,
            None => return false,
        };
        match self.last_sample {
            None => true,
            Some(last) => now.duration_since(last) >= interval,
        }
    }

    pub fn mark_sampled(&mut self, now: Instant) {
        self.last_sample = Some(now);
    }

    /// Whether this sample should be written to disk.
    ///
    /// Storage cadence is decoupled from decision cadence: a Hot burst makes
    /// hundreds of decisions but only needs to persist what changed, plus a
    /// heartbeat so gaps are distinguishable from downtime.
    pub fn should_persist(&self, now: Instant, changed: bool) -> bool {
        if changed {
            return true;
        }
        match self.last_persist {
            None => true,
            Some(last) => now.duration_since(last) >= self.cfg.heartbeat,
        }
    }

    pub fn mark_persisted(&mut self, now: Instant) {
        self.last_persist = Some(now);
    }

    /// Whether this game needs full book depth this tick. Depth is only fetched
    /// when it might actually be used, since it is the expensive part of a book
    /// request.
    pub fn wants_depth(&self) -> bool {
        matches!(self.phase, CadencePhase::Hot) || self.has_position
    }

    pub fn is_finished(&self) -> bool {
        self.phase == CadencePhase::Ended
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn live_state(home: i32, away: i32, remaining: f64) -> GameState {
        GameState {
            home_score: home,
            away_score: away,
            period: "Q3".into(),
            clock: "10:00".into(),
            status: GameStatus::InProgress,
            elapsed_minutes: Some(60.0 - remaining),
            home_red_cards: 0,
            away_red_cards: 0,
            under_review: false,
        }
    }

    fn ctl() -> CadenceController {
        CadenceController::new(Sport::AmericanFootball, CadenceConfig::default())
    }

    #[test]
    fn dormant_long_before_kickoff_and_silent() {
        let mut c = ctl();
        c.observe(Instant::now(), None, Some(3600.0));
        assert_eq!(c.phase(), CadencePhase::Dormant);
        assert_eq!(c.interval(), None);
        assert!(!c.should_sample(Instant::now()));
    }

    #[test]
    fn wakes_to_pregame_inside_the_lead_window() {
        let mut c = ctl();
        c.observe(Instant::now(), None, Some(300.0));
        assert_eq!(c.phase(), CadencePhase::PreGame);
        assert_eq!(c.interval(), Some(Duration::from_secs(5)));
    }

    #[test]
    fn live_with_nothing_happening_is_idle() {
        let mut c = ctl();
        let now = Instant::now();
        c.observe(now, Some(&live_state(0, 0, 30.0)), Some(-60.0));
        assert_eq!(c.phase(), CadencePhase::IdleInPlay);
        assert_eq!(c.interval(), Some(Duration::from_secs(2)));
    }

    #[test]
    fn a_score_promotes_to_hot() {
        let mut c = ctl();
        let now = Instant::now();
        c.observe(now, Some(&live_state(0, 0, 30.0)), Some(-60.0));
        assert_eq!(c.phase(), CadencePhase::IdleInPlay);

        let fired = c.observe(now, Some(&live_state(7, 0, 29.0)), Some(-60.0));
        assert!(fired, "a score must trigger a hot burst");
        assert_eq!(c.phase(), CadencePhase::Hot);
        assert_eq!(c.interval(), Some(Duration::from_millis(150)));
    }

    #[test]
    fn a_clock_tick_alone_does_not_promote_to_hot() {
        let mut c = ctl();
        let now = Instant::now();
        c.observe(now, Some(&live_state(3, 0, 30.0)), Some(-60.0));

        let mut later = live_state(3, 0, 29.0);
        later.clock = "09:00".into();
        let fired = c.observe(now, Some(&later), Some(-60.0));
        assert!(!fired, "clock movement is not an event");
        assert_eq!(c.phase(), CadencePhase::IdleInPlay);
    }

    #[test]
    fn hot_decays_back_to_idle_after_the_window() {
        let mut c = ctl();
        let t0 = Instant::now();
        c.observe(t0, Some(&live_state(0, 0, 30.0)), Some(-60.0));
        c.observe(t0, Some(&live_state(7, 0, 30.0)), Some(-60.0));
        assert_eq!(c.phase(), CadencePhase::Hot);

        // Past the hot window, with no new event.
        let t1 = t0 + Duration::from_secs(60);
        c.observe(t1, Some(&live_state(7, 0, 29.0)), Some(-60.0));
        assert_eq!(c.phase(), CadencePhase::IdleInPlay);
    }

    #[test]
    fn the_closing_minutes_stay_hot() {
        let mut c = ctl();
        let now = Instant::now();
        c.observe(now, Some(&live_state(21, 20, 3.0)), Some(-3000.0));
        assert_eq!(
            c.phase(),
            CadencePhase::Hot,
            "endgame must not be sampled at the idle rate"
        );
    }

    #[test]
    fn an_open_position_imposes_a_rate_floor_while_idle() {
        let mut c = ctl();
        let now = Instant::now();
        c.observe(now, Some(&live_state(0, 0, 30.0)), Some(-60.0));
        assert_eq!(c.interval(), Some(Duration::from_secs(2)));

        c.set_has_position(true);
        assert_eq!(
            c.interval(),
            Some(Duration::from_millis(500)),
            "a stop-loss cannot wait for the idle cadence"
        );
    }

    #[test]
    fn an_open_position_does_not_wake_a_finished_game() {
        let mut c = ctl();
        let now = Instant::now();
        let mut ended = live_state(21, 17, 0.0);
        ended.status = GameStatus::Final;
        c.observe(now, Some(&ended), Some(-4000.0));
        c.set_has_position(true);
        assert_eq!(c.phase(), CadencePhase::Ended);
        assert_eq!(c.interval(), None);
    }

    #[test]
    fn model_market_divergence_triggers_hot() {
        let mut c = ctl();
        let now = Instant::now();
        c.observe(now, Some(&live_state(0, 0, 30.0)), Some(-60.0));

        assert!(!c.observe_divergence(now, 0.52, 0.50), "small gap: no burst");
        assert!(c.observe_divergence(now, 0.62, 0.50), "large gap: burst");
    }

    #[test]
    fn sampling_respects_the_interval() {
        let mut c = ctl();
        let t0 = Instant::now();
        c.observe(t0, Some(&live_state(0, 0, 30.0)), Some(-60.0));

        assert!(c.should_sample(t0), "first sample is always due");
        c.mark_sampled(t0);
        assert!(!c.should_sample(t0 + Duration::from_millis(500)));
        assert!(c.should_sample(t0 + Duration::from_secs(3)));
    }

    #[test]
    fn persistence_is_change_driven_with_a_heartbeat() {
        let mut c = ctl();
        let t0 = Instant::now();

        assert!(c.should_persist(t0, false), "first sample persists");
        c.mark_persisted(t0);

        // Unchanged and inside the heartbeat: skip.
        assert!(!c.should_persist(t0 + Duration::from_secs(2), false));
        // Changed: always persist.
        assert!(c.should_persist(t0 + Duration::from_secs(2), true));
        // Heartbeat elapsed: persist even unchanged, so a gap in the file
        // means downtime rather than quiet.
        assert!(c.should_persist(t0 + Duration::from_secs(11), false));
    }

    #[test]
    fn depth_is_only_fetched_when_it_could_be_used() {
        let mut c = ctl();
        let t0 = Instant::now();
        c.observe(t0, Some(&live_state(0, 0, 30.0)), Some(-60.0));
        assert!(!c.wants_depth(), "idle does not need depth every tick");

        c.observe(t0, Some(&live_state(7, 0, 30.0)), Some(-60.0));
        assert!(c.wants_depth(), "hot needs depth");

        let t1 = t0 + Duration::from_secs(60);
        c.observe(t1, Some(&live_state(7, 0, 29.0)), Some(-60.0));
        c.set_has_position(true);
        assert!(c.wants_depth(), "an open position needs depth for exits");
    }

    #[test]
    fn a_final_game_stops_sampling() {
        let mut c = ctl();
        let mut s = live_state(21, 17, 0.0);
        s.status = GameStatus::Final;
        c.observe(Instant::now(), Some(&s), Some(-4000.0));
        assert!(c.is_finished());
        assert_eq!(c.interval(), None);
        assert!(!c.should_sample(Instant::now()));
    }

    #[test]
    fn a_review_flag_counts_as_a_material_event() {
        let mut c = ctl();
        let now = Instant::now();
        c.observe(now, Some(&live_state(7, 0, 20.0)), Some(-60.0));

        let mut under_review = live_state(7, 0, 20.0);
        under_review.under_review = true;
        assert!(
            c.observe(now, Some(&under_review), Some(-60.0)),
            "a review changes what the price should be and must wake the game"
        );
    }
}
