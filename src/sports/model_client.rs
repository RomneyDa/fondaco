//! Client for the Python model server.
//!
//! The server is asked **once per game** for a parameterisation, not once per
//! tick for a probability. Rust caches the rates and evaluates the in-play
//! probability locally on every tick.
//!
//! Every failure is soft and explicit: an unreachable server, an unknown team,
//! or an unparseable response all yield league-average rates flagged
//! `fitted: false`. That flag propagates to `FairValueSource::ModelFallback`,
//! which model-dependent strategies refuse to trade on — an "edge" measured
//! against parameters that know nothing about which team is better is an
//! artefact, not a signal.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use log::{debug, warn};
use reqwest::blocking::Client;
use serde::Deserialize;

use super::fair_value::GameParams;
use super::models::Sport;

pub const DEFAULT_MODEL_SERVER: &str = "http://127.0.0.1:8422";

#[derive(Debug, Clone, Deserialize)]
struct ParamsResponse {
    home_rate: f64,
    away_rate: f64,
    #[serde(default)]
    rho: f64,
    #[serde(default = "default_red_card_factor")]
    red_card_factor: f64,
    #[serde(default)]
    fitted: bool,
}

fn default_red_card_factor() -> f64 {
    0.75
}

/// Parameters plus whether they are team-specific.
#[derive(Debug, Clone, PartialEq)]
pub struct FetchedParams {
    pub params: GameParams,
    /// False when the rates are league averages rather than a real fit.
    pub fitted: bool,
}

#[derive(Clone)]
pub struct ModelClient {
    http: Client,
    base_url: String,
    /// Cache keyed by (league, home, away) — the fit only changes weekly.
    cache: Arc<Mutex<HashMap<String, FetchedParams>>>,
}

impl ModelClient {
    pub fn new(base_url: &str) -> Self {
        Self {
            http: Client::builder()
                .timeout(Duration::from_secs(5))
                .build()
                .expect("build model client"),
            base_url: base_url.trim_end_matches('/').to_string(),
            cache: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    fn cache_key(league: &str, home: &str, away: &str) -> String {
        format!("{league}|{home}|{away}")
    }

    /// Fetch rates for a fixture, caching the result.
    pub fn params_for(
        &self,
        league: &str,
        sport: Sport,
        home: &str,
        away: &str,
    ) -> FetchedParams {
        let key = Self::cache_key(league, home, away);
        if let Ok(cache) = self.cache.lock() {
            if let Some(hit) = cache.get(&key) {
                return hit.clone();
            }
        }

        let fetched = self
            .fetch(league, home, away)
            .unwrap_or_else(|| FetchedParams {
                params: GameParams::league_average(sport),
                fitted: false,
            });

        if let Ok(mut cache) = self.cache.lock() {
            cache.insert(key, fetched.clone());
        }
        fetched
    }

    fn fetch(&self, league: &str, home: &str, away: &str) -> Option<FetchedParams> {
        let url = format!("{}/params", self.base_url);
        let body = serde_json::json!({
            "league": league,
            "home": home,
            "away": away,
        });

        let resp = match self.http.post(&url).json(&body).send() {
            Ok(r) => r,
            Err(e) => {
                warn!("[MODEL] server unreachable ({e}) — falling back to league averages");
                return None;
            }
        };
        if !resp.status().is_success() {
            warn!("[MODEL] server returned {}", resp.status());
            return None;
        }

        let parsed: ParamsResponse = match resp.json() {
            Ok(p) => p,
            Err(e) => {
                warn!("[MODEL] unparseable response: {e}");
                return None;
            }
        };

        // Reject nonsense rather than trading on it.
        if !parsed.home_rate.is_finite()
            || !parsed.away_rate.is_finite()
            || parsed.home_rate < 0.0
            || parsed.away_rate < 0.0
            || parsed.home_rate > 100.0
            || parsed.away_rate > 100.0
        {
            warn!(
                "[MODEL] implausible rates home={} away={} — falling back",
                parsed.home_rate, parsed.away_rate
            );
            return None;
        }

        if !parsed.fitted {
            debug!("[MODEL] {league} {away} @ {home}: server has no fit for these teams");
        }

        Some(FetchedParams {
            params: GameParams {
                home_rate: parsed.home_rate,
                away_rate: parsed.away_rate,
                rho: parsed.rho.clamp(-0.99, 0.99),
                red_card_factor: parsed.red_card_factor.clamp(0.1, 1.0),
            },
            fitted: parsed.fitted,
        })
    }

    /// Whether the server is reachable.
    pub fn healthy(&self) -> bool {
        self.http
            .get(format!("{}/health", self.base_url))
            .timeout(Duration::from_secs(2))
            .send()
            .map(|r| r.status().is_success())
            .unwrap_or(false)
    }

    #[doc(hidden)]
    pub fn seed_cache_for_test(&self, league: &str, home: &str, away: &str, p: FetchedParams) {
        if let Ok(mut c) = self.cache.lock() {
            c.insert(Self::cache_key(league, home, away), p);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unreachable_server_yields_unfitted_league_averages() {
        // Port 1 is reserved and will not be listening.
        let c = ModelClient::new("http://127.0.0.1:1");
        let f = c.params_for("epl", Sport::Soccer, "Arsenal", "Chelsea");

        assert!(!f.fitted, "must not claim a fit it does not have");
        let avg = GameParams::league_average(Sport::Soccer);
        assert_eq!(f.params.home_rate, avg.home_rate);
        assert_eq!(f.params.away_rate, avg.away_rate);
    }

    #[test]
    fn fallback_params_are_marked_untradeable_downstream() {
        let c = ModelClient::new("http://127.0.0.1:1");
        let f = c.params_for("nfl", Sport::AmericanFootball, "A", "B");
        // This is the flag that stops ModelEdge from trading an artefact.
        assert!(!f.fitted);
        assert!(!crate::sports::fair_value::FairValueSource::ModelFallback.is_tradeable());
    }

    #[test]
    fn results_are_cached_per_fixture() {
        let c = ModelClient::new("http://127.0.0.1:1");
        let seeded = FetchedParams {
            params: GameParams {
                home_rate: 2.5,
                away_rate: 0.9,
                rho: -0.1,
                red_card_factor: 0.7,
            },
            fitted: true,
        };
        c.seed_cache_for_test("epl", "Arsenal", "Chelsea", seeded.clone());

        // Served from cache without touching the (dead) server.
        let got = c.params_for("epl", Sport::Soccer, "Arsenal", "Chelsea");
        assert_eq!(got, seeded);

        // A different fixture misses the cache and falls back.
        let other = c.params_for("epl", Sport::Soccer, "Spurs", "Chelsea");
        assert!(!other.fitted);
    }

    #[test]
    fn cache_keys_distinguish_home_from_away() {
        let a = ModelClient::cache_key("epl", "Arsenal", "Chelsea");
        let b = ModelClient::cache_key("epl", "Chelsea", "Arsenal");
        assert_ne!(a, b, "home advantage makes the fixtures different");
    }

    #[test]
    fn a_dead_server_is_not_healthy() {
        assert!(!ModelClient::new("http://127.0.0.1:1").healthy());
    }
}
