//! Example strategy implementations for up/down prediction markets.
//!
//! These strategies are provided for illustration and experimentation only.
//! There is no evidence that any of them are profitable. Write your own
//! strategies in this directory — files here (except the bundled examples)
//! are gitignored by default so your work stays private.

pub mod poisson_kelly;
pub mod poisson_kelly_dynamic;
pub mod drift_sniper;
pub mod gap_fader;
pub mod regime_sniper;
pub mod trend_drift;
pub mod mean_rev_fader;
pub mod late_sniper_cb;


use crate::strategy::Strategy;
use std::collections::HashMap;

/// Registry of all available strategies.
pub fn all_strategies() -> HashMap<String, fn() -> Box<dyn Strategy>> {
    let mut m: HashMap<String, fn() -> Box<dyn Strategy>> = HashMap::new();
    m.insert("PoissonKelly".to_string(), || Box::new(poisson_kelly::PoissonKelly::default()));
    m.insert("PoissonKellyDynamic".to_string(), || Box::new(poisson_kelly_dynamic::PoissonKellyDynamic::default()));
    m.insert("DriftSniper".to_string(), || Box::new(drift_sniper::DriftSniper::default()));
    m.insert("GapFader".to_string(), || Box::new(gap_fader::GapFader::default()));
    m.insert("RegimeSniper".to_string(), || Box::new(regime_sniper::RegimeSniper::default()));
    m.insert("TrendDrift".to_string(), || Box::new(trend_drift::TrendDrift::default()));
    m.insert("MeanRevFader".to_string(), || Box::new(mean_rev_fader::MeanRevFader::default()));
    m.insert("LateSniperCB".to_string(), || Box::new(late_sniper_cb::LateSniperCB::default()));
    m
}
