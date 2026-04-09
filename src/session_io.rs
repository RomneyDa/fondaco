//! Session file I/O: reading/writing session JSON, live session, PID files, collector state.

use chrono::Utc;
use std::fs;
use std::io::Write as IoWrite;
use std::path::PathBuf;

use crate::models::{SessionData, SessionSummary};

pub fn sessions_dir(output_dir: &str, slug_key: &str) -> PathBuf {
    PathBuf::from(output_dir)
        .join("markets")
        .join(slug_key)
        .join("sessions")
}

pub fn session_exists(output_dir: &str, slug_key: &str, window_start_utc: &str) -> bool {
    let dir = sessions_dir(output_dir, slug_key);
    // Filename is window_start_utc with colons replaced by dashes + ".json"
    let filename = window_start_utc.replace(":", "-").replace(" ", "T") + ".json";
    dir.join(&filename).exists()
}

pub fn save_session(output_dir: &str, slug_key: &str, session: &SessionData) -> Option<String> {
    let dir = sessions_dir(output_dir, slug_key);
    fs::create_dir_all(&dir).ok()?;

    let window_start = session
        .metadata
        .window_start_utc
        .as_deref()
        .unwrap_or("unknown");
    let filename = window_start
        .replace(":", "-")
        .replace(" ", "T")
        + ".json";
    let filepath = dir.join(&filename);

    let json = serde_json::to_string_pretty(session).ok()?;

    let tmp_path = filepath.with_extension("json.tmp");
    {
        let mut f = fs::File::create(&tmp_path).ok()?;
        f.write_all(json.as_bytes()).ok()?;
        f.flush().ok()?;
    }
    fs::rename(&tmp_path, &filepath).ok()?;

    // Write lightweight summary sidecar
    if let Some(summary) = session_to_summary(session) {
        let summary_path = filepath.with_extension("summary.json");
        let summary_tmp = summary_path.with_extension("json.tmp");
        if let Ok(summary_json) = serde_json::to_string(&summary) {
            if let Ok(mut f) = fs::File::create(&summary_tmp) {
                let _ = f.write_all(summary_json.as_bytes());
                let _ = f.flush();
                let _ = fs::rename(&summary_tmp, &summary_path);
            }
        }
    }

    Some(filepath.to_string_lossy().to_string())
}

/// Extract a lightweight summary from a full session, if it has valid data.
fn session_to_summary(session: &SessionData) -> Option<SessionSummary> {
    let window_start = session.metadata.window_start_utc.as_deref()?;
    if window_start.is_empty() {
        return None;
    }
    let initial_btc = session.initial_btc_price_usd.filter(|v| *v > 0.0)?;
    let final_btc = session.final_btc_price_usd.filter(|v| *v > 0.0)?;
    let outcome = session.outcome.as_deref().filter(|o| !o.is_empty())?;
    Some(SessionSummary {
        window_start: window_start.to_string(),
        initial_btc,
        final_btc,
        outcome: outcome.to_string(),
    })
}

pub fn write_live_session(output_dir: &str, slug_key: &str, session: &SessionData) {
    let dir = PathBuf::from(output_dir)
        .join("markets")
        .join(slug_key)
        .join("live");
    if fs::create_dir_all(&dir).is_err() {
        return;
    }
    let filepath = dir.join("session.json");
    let tmp_path = filepath.with_extension("json.tmp");
    if let Ok(json) = serde_json::to_string(session) {
        if let Ok(mut f) = fs::File::create(&tmp_path) {
            let _ = f.write_all(json.as_bytes());
            let _ = f.flush();
            let _ = fs::rename(&tmp_path, &filepath);
        }
    }
}

pub fn write_live_status(output_dir: &str, slug_key: &str, status: &str, next_window_utc: Option<&str>) {
    let dir = PathBuf::from(output_dir)
        .join("markets")
        .join(slug_key)
        .join("live");
    if fs::create_dir_all(&dir).is_err() {
        return;
    }
    let filepath = dir.join("session.json");
    let tmp_path = filepath.with_extension("json.tmp");
    let data = serde_json::json!({
        "status": status,
        "slug_key": slug_key,
        "timestamp": Utc::now().to_rfc3339(),
        "next_window_utc": next_window_utc,
    });
    if let Ok(mut f) = fs::File::create(&tmp_path) {
        let _ = serde_json::to_writer(&mut f, &data);
        let _ = f.flush();
        let _ = fs::rename(&tmp_path, &filepath);
    }
}

pub fn clear_live_session(output_dir: &str, slug_key: &str) {
    let filepath = PathBuf::from(output_dir)
        .join("markets")
        .join(slug_key)
        .join("live")
        .join("session.json");
    let _ = fs::remove_file(filepath);
}

pub fn write_pid_file(path: &str) {
    let pid = std::process::id();
    let tmp = format!("{}.tmp", path);
    if let Ok(mut f) = fs::File::create(&tmp) {
        let _ = write!(f, "{}", pid);
        let _ = fs::rename(&tmp, path);
    }
}

pub fn remove_file_silent(path: &str) {
    let _ = fs::remove_file(path);
}

pub fn write_collector_state(output_dir: &str, slug_key: &str, session_count: u64, state: &str) {
    let state_path = PathBuf::from(output_dir)
        .join("markets")
        .join(slug_key)
        .join("collector_state.json");
    if let Some(parent) = state_path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let data = serde_json::json!({
        "pid": std::process::id(),
        "slug_key": slug_key,
        "session_count": session_count,
        "state": state,
        "updated_at": Utc::now().to_rfc3339(),
    });
    let tmp = format!("{}.tmp", state_path.display());
    if let Ok(mut f) = fs::File::create(&tmp) {
        let _ = serde_json::to_writer_pretty(&mut f, &data);
        let _ = fs::rename(&tmp, &state_path);
    }
}

pub fn write_collector_state_with_book_source(
    output_dir: &str,
    slug_key: &str,
    session_count: u64,
    state: &str,
    book_source: &str,
) {
    let state_path = PathBuf::from(output_dir)
        .join("markets")
        .join(slug_key)
        .join("collector_state.json");
    if let Some(parent) = state_path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let data = serde_json::json!({
        "pid": std::process::id(),
        "slug_key": slug_key,
        "session_count": session_count,
        "state": state,
        "book_source": book_source,
        "updated_at": Utc::now().to_rfc3339(),
    });
    let tmp = format!("{}.tmp", state_path.display());
    if let Ok(mut f) = fs::File::create(&tmp) {
        let _ = serde_json::to_writer_pretty(&mut f, &data);
        let _ = fs::rename(&tmp, &state_path);
    }
}

pub fn remove_collector_state(output_dir: &str, slug_key: &str) {
    let state_path = PathBuf::from(output_dir)
        .join("markets")
        .join(slug_key)
        .join("collector_state.json");
    let _ = fs::remove_file(state_path);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::{MarketMetadata, Sample};
    use tempfile::tempdir;

    fn make_test_session(window_start: &str) -> SessionData {
        SessionData {
            metadata: MarketMetadata {
                event_id: Some("e1".to_string()),
                event_slug: Some("btc-updown-5m-123".to_string()),
                event_title: Some("BTC Up/Down".to_string()),
                condition_id: Some("c1".to_string()),
                up_token_id: Some("t1".to_string()),
                down_token_id: Some("t2".to_string()),
                window_start_utc: Some(window_start.to_string()),
                window_end_utc: Some("2026-01-01T00:05:00Z".to_string()),
                resolution_source: Some("chainlink".to_string()),
            },
            initial_btc_price_usd: Some(60000.0),
            final_btc_price_usd: Some(60100.0),
            outcome: Some("Up".to_string()),
            outcome_prices: Some(vec!["1.00".to_string(), "0.00".to_string()]),
            samples: vec![Sample {
                timestamp_utc: Some("2026-01-01T00:00:00Z".to_string()),
                elapsed_seconds: 0.0,
                btc_price_usd: Some(60000.0),
                up_token_midpoint: Some(0.50),
                down_token_midpoint: Some(0.50),
                up_token_best_bid: Some(0.01),
                up_token_best_ask: Some(0.99),
                down_token_best_bid: Some(0.01),
                down_token_best_ask: Some(0.99),
                server_timestamp: None,
            }],
            collection_started_utc: String::new(),
            collection_ended_utc: String::new(),
            error_count: 0,
            errors: vec![],
            source_file: String::new(),
            _prior_summaries: None,
        }
    }

    #[test]
    fn test_sessions_dir_path() {
        let path = sessions_dir("/data", "btc-updown-5m");
        assert_eq!(
            path,
            PathBuf::from("/data/markets/btc-updown-5m/sessions")
        );
    }

    #[test]
    fn test_session_exists_not_found() {
        let tmp = tempdir().unwrap();
        let out = tmp.path().to_str().unwrap();
        assert!(!session_exists(out, "btc-updown-5m", "2026-01-01T00:00:00Z"));
    }

    #[test]
    fn test_save_and_exists() {
        let tmp = tempdir().unwrap();
        let out = tmp.path().to_str().unwrap();
        let slug = "btc-updown-5m";
        let window = "2026-01-01T00:00:00Z";

        let session = make_test_session(window);
        let result = save_session(out, slug, &session);
        assert!(result.is_some(), "save_session should return Some(path)");

        assert!(session_exists(out, slug, window));

        // Verify the saved file is valid JSON
        let saved_path = result.unwrap();
        let content = fs::read_to_string(&saved_path).unwrap();
        let parsed: SessionData = serde_json::from_str(&content).unwrap();
        assert_eq!(parsed.outcome.as_deref(), Some("Up"));
        assert_eq!(parsed.samples.len(), 1);

        // Verify summary sidecar was written
        let summary_path = PathBuf::from(&saved_path).with_extension("summary.json");
        assert!(summary_path.exists(), "summary sidecar should exist");
        let summary_content = fs::read_to_string(&summary_path).unwrap();
        let summary: SessionSummary = serde_json::from_str(&summary_content).unwrap();
        assert_eq!(summary.window_start, window);
        assert_eq!(summary.outcome, "Up");
        assert_eq!(summary.initial_btc, 60000.0);
        assert_eq!(summary.final_btc, 60100.0);
    }

    #[test]
    fn test_write_live_session_and_clear() {
        let tmp = tempdir().unwrap();
        let out = tmp.path().to_str().unwrap();
        let slug = "btc-updown-5m";

        let session = make_test_session("2026-01-01T00:00:00Z");
        write_live_session(out, slug, &session);

        let live_path = tmp
            .path()
            .join("markets")
            .join(slug)
            .join("live")
            .join("session.json");
        assert!(live_path.exists(), "live session file should exist");

        // Verify it's valid JSON
        let content = fs::read_to_string(&live_path).unwrap();
        let parsed: SessionData = serde_json::from_str(&content).unwrap();
        assert_eq!(parsed.outcome.as_deref(), Some("Up"));

        clear_live_session(out, slug);
        assert!(!live_path.exists(), "live session file should be removed");
    }

    #[test]
    fn test_write_pid_file() {
        let tmp = tempdir().unwrap();
        let pid_path = tmp.path().join("test.pid");
        let pid_str = pid_path.to_str().unwrap();

        write_pid_file(pid_str);

        assert!(pid_path.exists(), "PID file should exist");
        let content = fs::read_to_string(&pid_path).unwrap();
        let written_pid: u32 = content.trim().parse().unwrap();
        assert_eq!(written_pid, std::process::id());
    }

    #[test]
    fn test_remove_file_silent_nonexistent() {
        // Should not panic when removing a file that doesn't exist
        remove_file_silent("/tmp/nonexistent_file_fondaco_test_12345.txt");
    }

    #[test]
    fn test_write_and_remove_collector_state() {
        let tmp = tempdir().unwrap();
        let out = tmp.path().to_str().unwrap();
        let slug = "btc-updown-5m";

        write_collector_state(out, slug, 42, "collecting");

        let state_path = tmp
            .path()
            .join("markets")
            .join(slug)
            .join("collector_state.json");
        assert!(state_path.exists(), "collector state file should exist");

        let content = fs::read_to_string(&state_path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&content).unwrap();
        assert_eq!(parsed["slug_key"].as_str(), Some(slug));
        assert_eq!(parsed["session_count"].as_u64(), Some(42));
        assert_eq!(parsed["state"].as_str(), Some("collecting"));
        assert!(parsed["pid"].as_u64().is_some());
        assert!(parsed["updated_at"].as_str().is_some());

        remove_collector_state(out, slug);
        assert!(
            !state_path.exists(),
            "collector state file should be removed"
        );
    }
}
