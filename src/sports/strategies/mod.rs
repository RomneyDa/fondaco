//! Example sports strategies.
//!
//! As with the crypto strategies, these are illustrative. There is no evidence
//! any of them is profitable.
//!
//! Every one carries **at most three tunable parameters**, and that is a hard
//! design rule rather than a coincidence. The NFL plays 272 regular-season
//! games a year; a three-point grid over six parameters is 729 variants, and
//! picking the best of those against a few hundred games fits noise with
//! near-certainty. `metrics::sample_size_warning` enforces the same point at
//! runtime.

pub mod stale_book;
pub mod model_edge;

use crate::sports::strategy::SportsStrategy;
use std::collections::HashMap;

/// Registry of sports strategies.
pub fn all_sports_strategies() -> HashMap<String, fn() -> Box<dyn SportsStrategy>> {
    let mut m: HashMap<String, fn() -> Box<dyn SportsStrategy>> = HashMap::new();
    m.insert("StaleBook".to_string(), || {
        Box::new(stale_book::StaleBook::default())
    });
    m.insert("ModelEdge".to_string(), || {
        Box::new(model_edge::ModelEdge::default())
    });
    m
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_registered_strategy_respects_the_parameter_budget() {
        for (name, factory) in all_sports_strategies() {
            let s = factory();
            assert!(
                s.tunable_count() <= 3,
                "{name} has {} tunable params; sports sample sizes cannot support \
                 more than 3",
                s.tunable_count()
            );
        }
    }

    #[test]
    fn registry_names_match_the_strategies_they_build() {
        for (name, factory) in all_sports_strategies() {
            assert_eq!(factory().name(), name);
        }
    }
}
