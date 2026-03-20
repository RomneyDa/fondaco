//! Shared math utilities for trading strategies.

use crate::defaults::BPS_DIVISOR;

/// Standard normal CDF via erf approximation.
pub fn norm_cdf(x: f64) -> f64 {
    0.5 * (1.0 + erf(x / std::f64::consts::SQRT_2))
}

/// Error function approximation (Abramowitz and Stegun, maximum error ~1.5e-7).
fn erf(x: f64) -> f64 {
    let a1 = 0.254829592;
    let a2 = -0.284496736;
    let a3 = 1.421413741;
    let a4 = -1.453152027;
    let a5 = 1.061405429;
    let p = 0.3275911;

    let sign = if x < 0.0 { -1.0 } else { 1.0 };
    let x = x.abs();
    let t = 1.0 / (1.0 + p * x);
    let y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * (-x * x).exp();
    sign * y
}

/// OLS slope of prices vs tick index, returned in bps/tick.
pub fn ols_slope_bps(prices: &[f64], window: usize) -> Option<f64> {
    if prices.len() < window {
        return None;
    }
    let recent = &prices[prices.len() - window..];
    let base = recent[0];
    if base <= 0.0 {
        return None;
    }
    let n = window as f64;
    let sum_x = n * (n - 1.0) / 2.0;
    let sum_x2 = n * (n - 1.0) * (2.0 * n - 1.0) / 6.0;
    let sum_y: f64 = recent.iter().sum();
    let sum_xy: f64 = recent.iter().enumerate().map(|(i, &y)| i as f64 * y).sum();
    let denom = n * sum_x2 - sum_x * sum_x;
    if denom == 0.0 {
        return None;
    }
    let slope = (n * sum_xy - sum_x * sum_y) / denom;
    Some((slope / base) * BPS_DIVISOR)
}

/// OLS slope of prices vs time, returned in bps/second.
pub fn ols_slope_bps_timed(times: &[f64], prices: &[f64]) -> Option<f64> {
    let n = prices.len();
    if n < 2 || times.len() != n {
        return None;
    }
    let base = prices[0];
    if base <= 0.0 {
        return None;
    }
    let sum_x: f64 = times.iter().sum();
    let sum_x2: f64 = times.iter().map(|t| t * t).sum();
    let sum_y: f64 = prices.iter().sum();
    let sum_xy: f64 = times.iter().zip(prices.iter()).map(|(t, p)| t * p).sum();
    let nf = n as f64;
    let denom = nf * sum_x2 - sum_x * sum_x;
    if denom == 0.0 {
        return None;
    }
    let slope = (nf * sum_xy - sum_x * sum_y) / denom;
    Some((slope / base) * BPS_DIVISOR)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_norm_cdf() {
        assert!((norm_cdf(0.0) - 0.5).abs() < 1e-6);
        assert!((norm_cdf(1.0) - 0.8413).abs() < 1e-3);
        assert!((norm_cdf(-1.0) - 0.1587).abs() < 1e-3);
        // Large values
        assert!(norm_cdf(5.0) > 0.99999);
        assert!(norm_cdf(-5.0) < 0.00001);
    }

    #[test]
    fn test_ols_slope_bps() {
        // Flat prices → slope 0
        let prices = vec![100.0, 100.0, 100.0, 100.0];
        let slope = ols_slope_bps(&prices, 4).unwrap();
        assert!(slope.abs() < 1e-10);

        // Increasing prices
        let prices = vec![100.0, 101.0, 102.0, 103.0];
        let slope = ols_slope_bps(&prices, 4).unwrap();
        assert!(slope > 0.0);

        // Insufficient data
        assert!(ols_slope_bps(&[100.0], 3).is_none());
    }

    #[test]
    fn test_ols_slope_bps_timed() {
        let times = vec![0.0, 1.0, 2.0, 3.0];
        let prices = vec![100.0, 101.0, 102.0, 103.0];
        let slope = ols_slope_bps_timed(&times, &prices).unwrap();
        assert!(slope > 0.0);

        // Insufficient data
        assert!(ols_slope_bps_timed(&[0.0], &[100.0]).is_none());

        // Mismatched lengths
        assert!(ols_slope_bps_timed(&[0.0, 1.0], &[100.0]).is_none());
    }
}
