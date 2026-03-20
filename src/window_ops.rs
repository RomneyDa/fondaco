//! Window timing utilities: waiting for the next trading window to start.

use chrono::DateTime;
use log::info;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// Grace period for cold starts — if we're within this many seconds of a
/// window boundary with no `after_window_ts`, use the current window.
const WINDOW_GRACE_SECONDS: u64 = 3;

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

/// Compute a monotonic `Instant` corresponding to a specific unix timestamp.
/// Uses the current time to bridge between wall clock and monotonic clock.
fn instant_at_unix(target_ts: u64) -> Instant {
    let now_sys = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap();
    let target_dur = Duration::from_secs(target_ts);
    let now_mono = Instant::now();
    if target_dur > now_sys {
        now_mono + (target_dur - now_sys)
    } else {
        now_mono - (now_sys - target_dur)
    }
}

/// Wait for the next window boundary. Returns the window timestamp or None on shutdown.
///
/// When `after_window_ts` is provided (i.e. we just finished a session), we target
/// `after_window_ts + window_duration` exactly. If we're already past that target,
/// we jump in immediately — no missed windows from post-session cleanup delays.
pub fn wait_for_next_window(
    window_duration: u64,
    running: &AtomicBool,
    after_window_ts: Option<u64>,
) -> Option<u64> {
    if !running.load(Ordering::SeqCst) {
        return None;
    }

    if let Some(after_ts) = after_window_ts {
        let target = after_ts + window_duration;
        let now = now_unix();

        if now >= target + window_duration {
            // We're way past the target window — fall through to cold-start logic
        } else if now >= target {
            // Target window already started — jump in immediately
            return Some(target);
        } else {
            // Target window hasn't started yet — sleep precisely to it
            let wait_secs = target - now;
            let target_dt = DateTime::from_timestamp(target as i64, 0).unwrap_or_default();
            info!(
                "Waiting for next window ({} in {}s)",
                target_dt.format("%H:%M:%S UTC"),
                wait_secs
            );
            sleep_until(instant_at_unix(target), running);
            if !running.load(Ordering::SeqCst) {
                return None;
            }
            return Some(target);
        }
    }

    // Cold start (no after_window_ts, or we're way past the target)
    let now = now_unix();
    let current_window = (now / window_duration) * window_duration;

    // If we're in the first few seconds, use the current window
    if now - current_window < WINDOW_GRACE_SECONDS {
        return Some(current_window);
    }

    // Otherwise wait for the next window
    let next_window = current_window + window_duration;
    let wait_secs = next_window - now;
    let next_dt = DateTime::from_timestamp(next_window as i64, 0).unwrap_or_default();
    info!(
        "Waiting for next window ({} in {}s)",
        next_dt.format("%H:%M:%S UTC"),
        wait_secs
    );
    sleep_until(instant_at_unix(next_window), running);
    if !running.load(Ordering::SeqCst) {
        return None;
    }
    Some(next_window)
}

/// Returns the target window timestamp that `wait_for_next_window` will return,
/// without actually waiting. Used to pre-fetch metadata.
pub fn predict_next_window(window_duration: u64, after_window_ts: Option<u64>) -> u64 {
    let now = now_unix();

    if let Some(after_ts) = after_window_ts {
        let target = after_ts + window_duration;
        if now < target + window_duration {
            return target;
        }
    }

    let current_window = (now / window_duration) * window_duration;
    if now - current_window < WINDOW_GRACE_SECONDS {
        current_window
    } else {
        current_window + window_duration
    }
}

/// Sleep until a specific `Instant`, checking the shutdown flag periodically.
pub fn sleep_until(deadline: Instant, running: &AtomicBool) {
    while Instant::now() < deadline && running.load(Ordering::SeqCst) {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        // Check shutdown every 250ms, but don't overshoot the deadline
        thread::sleep(remaining.min(Duration::from_millis(250)));
    }
}

pub fn sleep_interruptible(dur: Duration, running: &AtomicBool) {
    sleep_until(Instant::now() + dur, running);
}
