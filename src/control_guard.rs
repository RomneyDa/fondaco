//! Decisions for the local process-control API.
//!
//! A pid that does not fit in a positive `i32` must never reach `kill`.
//! The same cast turns `0` into a process-group signal and `u32::MAX`
//! into `-1`, which signals every process the user may signal.

/// Loopback names this process may bind without a shared token.
pub fn is_loopback(host: &str) -> bool {
    matches!(host, "127.0.0.1" | "localhost" | "::1")
}

/// Non-loopback binds require a non-empty token.
pub fn bind_allowed(bind: &str, token: Option<&str>) -> Result<(), &'static str> {
    let token_set = token.is_some_and(|value| !value.trim().is_empty());
    if is_loopback(bind) || token_set {
        Ok(())
    } else {
        Err("refusing to listen on a non-loopback address without a control token")
    }
}

/// Pid that is safe to pass to `kill`.
pub fn signalable_pid(raw: u64) -> Option<i32> {
    if raw == 0 || raw > i32::MAX as u64 {
        return None;
    }
    let pid = raw as i32;
    if pid <= 0 {
        None
    } else {
        Some(pid)
    }
}

fn single_component(raw: &str) -> Option<&str> {
    if raw.is_empty() || raw.len() > 128 {
        return None;
    }
    if raw == "." || raw == ".." || raw.contains("..") || raw.contains('/') || raw.contains('\\') {
        return None;
    }
    let mut chars = raw.chars();
    if !chars.next().is_some_and(|c| c.is_ascii_alphanumeric()) {
        return None;
    }
    if !chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')) {
        return None;
    }
    Some(raw)
}

/// Market key from a slug prefix. One trailing-dash run is stripped.
pub fn slug_key(prefix: &str) -> Option<&str> {
    single_component(prefix.trim().trim_end_matches('-'))
}

/// Strategy name used in pid files and argv. Not a path.
pub fn strategy_name(name: &str) -> Option<&str> {
    single_component(name.trim())
}

/// A caller-supplied pid may be signaled only when it is safe and already ours.
pub fn owned_signal_pid(raw: u64, owned: &[u32]) -> Option<u32> {
    let pid = signalable_pid(raw)? as u32;
    if owned.contains(&pid) {
        Some(pid)
    } else {
        None
    }
}

/// Constant-time compare for a configured control token.
pub fn token_matches(expected: &str, got: &str) -> bool {
    let a = expected.as_bytes();
    let b = got.as_bytes();
    let mut diff = a.len() ^ b.len();
    let n = a.len().max(b.len());
    for i in 0..n {
        let x = *a.get(i).unwrap_or(&0);
        let y = *b.get(i).unwrap_or(&0);
        diff |= usize::from(x ^ y);
    }
    diff == 0 && !expected.is_empty()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_pids_that_become_group_or_all_process_signals() {
        assert_eq!(signalable_pid(0), None);
        assert_eq!(signalable_pid(u32::MAX as u64), None);
        assert_eq!(signalable_pid(i32::MAX as u64 + 1), None);
        assert_eq!(signalable_pid(1), Some(1));
        assert_eq!(signalable_pid(i32::MAX as u64), Some(i32::MAX));
    }

    #[test]
    fn caller_pid_must_be_one_we_recorded() {
        assert_eq!(owned_signal_pid(42, &[42, 7]), Some(42));
        assert_eq!(owned_signal_pid(42, &[7]), None);
        assert_eq!(owned_signal_pid(u32::MAX as u64, &[u32::MAX]), None);
        assert_eq!(owned_signal_pid(0, &[0]), None);
    }

    #[test]
    fn slug_and_strategy_are_single_components() {
        assert_eq!(slug_key("btc-updown-5m-"), Some("btc-updown-5m"));
        assert_eq!(strategy_name("PoissonKelly"), Some("PoissonKelly"));
        assert_eq!(slug_key("../etc"), None);
        assert_eq!(slug_key("btc/up"), None);
        assert_eq!(slug_key(".."), None);
        assert_eq!(slug_key(""), None);
        assert_eq!(strategy_name("a\\b"), None);
        assert_eq!(strategy_name(".hidden"), None);
    }

    #[test]
    fn public_bind_requires_a_token() {
        assert!(bind_allowed("127.0.0.1", None).is_ok());
        assert!(bind_allowed("localhost", Some("")).is_ok());
        assert!(bind_allowed("0.0.0.0", None).is_err());
        assert!(bind_allowed("0.0.0.0", Some("")).is_err());
        assert!(bind_allowed("0.0.0.0", Some("s3cret")).is_ok());
    }

    #[test]
    fn token_compare_rejects_different_lengths_and_values() {
        assert!(token_matches("abc", "abc"));
        assert!(!token_matches("abc", "abd"));
        assert!(!token_matches("abc", "abcd"));
        assert!(!token_matches("", ""));
    }
}
