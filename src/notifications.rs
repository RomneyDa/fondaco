//! Notification system — routes events to configured mediums (desktop, ntfy).
//!
//! Configuration lives in `settings.json` at the project root. Each medium has an
//! `enabled` flag and a list of event types it subscribes to.
//!
//! Supported event types (hierarchical — parent catches all children):
//!   - `trades`          — all trades (superset of buys+sells)
//!   - `trades:buys`     — buy executions
//!   - `trades:sells`    — sell executions
//!   - `session:end`     — any session resolution (superset of win+loss)
//!   - `session:end:win` — session resolved with positive PnL
//!   - `session:end:loss`— session resolved with negative PnL
//!   - `error`           — trade failures, process errors
//!   - `process:start`   — collector/worker started
//!   - `process:stop`    — collector/worker stopped
//!   - `clob_down`       — CLOB flat line detected (midpoints stale)
//!   - `report:hourly`   — periodic summary (future)

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::fs;
use std::path::PathBuf;

use log::{debug, warn};
use serde::Deserialize;

// ---------------------------------------------------------------------------
// Event types
// ---------------------------------------------------------------------------

/// Notification event categories.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum NotificationEvent {
    TradeBuy,
    TradeSell,
    SessionEndWin,
    SessionEndLoss,
    Error,
    ProcessStart,
    ProcessStop,
    ClobDown,
    ReportHourly,
    // Sports-specific
    GameStart,
    GameEnd,
    SportsScore,
    /// The book has not repriced after a scoring event — the layer-0 signal.
    SportsStaleBook,
    /// A play or goal is under official review; trading is suspended.
    SportsReviewPending,
    /// The portfolio allocator refused or throttled a position.
    RiskExposureLimit,
}

impl NotificationEvent {
    /// Returns all event type strings that this event matches (for hierarchical matching).
    fn matches(&self) -> &'static [&'static str] {
        match self {
            Self::TradeBuy => &["trades:buys", "trades"],
            Self::TradeSell => &["trades:sells", "trades"],
            Self::SessionEndWin => &["session:end:win", "session:end"],
            Self::SessionEndLoss => &["session:end:loss", "session:end"],
            Self::Error => &["error"],
            Self::ProcessStart => &["process:start"],
            Self::ProcessStop => &["process:stop"],
            Self::ClobDown => &["clob_down"],
            Self::ReportHourly => &["report:hourly"],
            Self::GameStart => &["sports:game:start", "sports:game", "sports"],
            Self::GameEnd => &["sports:game:end", "sports:game", "sports"],
            Self::SportsScore => &["sports:score", "sports"],
            Self::SportsStaleBook => &["sports:stale_book", "sports"],
            Self::SportsReviewPending => &["sports:review_pending", "sports"],
            Self::RiskExposureLimit => &["risk:exposure_limit", "risk"],
        }
    }

    /// ntfy priority: error is urgent, losses are high, rest is default.
    fn ntfy_priority(&self) -> &'static str {
        match self {
            Self::Error => "urgent",
            Self::SessionEndLoss => "high",
            Self::ProcessStop => "high",
            Self::ClobDown => "urgent",
            // A stale book is a fleeting opportunity — it is worth interrupting
            // for, unlike a routine score update.
            Self::SportsStaleBook => "high",
            Self::RiskExposureLimit => "high",
            _ => "default",
        }
    }

    /// ntfy tag (renders as emoji in the app).
    fn ntfy_tag(&self) -> &'static str {
        match self {
            Self::TradeBuy => "chart_with_upwards_trend",
            Self::TradeSell => "chart_with_downwards_trend",
            Self::SessionEndWin => "white_check_mark",
            Self::SessionEndLoss => "x",
            Self::Error => "warning",
            Self::ProcessStart => "rocket",
            Self::ProcessStop => "stop_sign",
            Self::ClobDown => "rotating_light",
            Self::ReportHourly => "bar_chart",
            Self::GameStart => "stadium",
            Self::GameEnd => "checkered_flag",
            Self::SportsScore => "soccer",
            Self::SportsStaleBook => "hourglass_flowing_sand",
            Self::SportsReviewPending => "mag",
            Self::RiskExposureLimit => "shield",
        }
    }
}

impl fmt::Display for NotificationEvent {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let s = match self {
            Self::TradeBuy => "Trade Buy",
            Self::TradeSell => "Trade Sell",
            Self::SessionEndWin => "Session Win",
            Self::SessionEndLoss => "Session Loss",
            Self::Error => "Error",
            Self::ProcessStart => "Process Started",
            Self::ProcessStop => "Process Stopped",
            Self::ClobDown => "CLOB Down",
            Self::ReportHourly => "Hourly Report",
            Self::GameStart => "Game Started",
            Self::GameEnd => "Game Ended",
            Self::SportsScore => "Score Update",
            Self::SportsStaleBook => "Stale Book",
            Self::SportsReviewPending => "Under Review",
            Self::RiskExposureLimit => "Exposure Limit",
        };
        write!(f, "{}", s)
    }
}

// ---------------------------------------------------------------------------
// Notification payload
// ---------------------------------------------------------------------------

/// A notification to be sent.
pub struct Notification {
    pub event: NotificationEvent,
    pub title: String,
    pub body: String,
}

impl Notification {
    pub fn new(event: NotificationEvent, title: impl Into<String>, body: impl Into<String>) -> Self {
        Self {
            event,
            title: title.into(),
            body: body.into(),
        }
    }
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, Default)]
struct SettingsFile {
    #[serde(default)]
    notifications: NotificationsConfig,
}

#[derive(Debug, Deserialize, Default)]
struct NotificationsConfig {
    #[serde(default)]
    desktop: MediumConfig,
    #[serde(default)]
    ntfy: NtfyConfig,
}

#[derive(Debug, Deserialize, Default, Clone)]
struct MediumConfig {
    #[serde(default)]
    enabled: bool,
    #[serde(default)]
    events: Vec<String>,
}

#[derive(Debug, Deserialize, Default, Clone)]
struct NtfyConfig {
    #[serde(default)]
    enabled: bool,
    #[serde(default)]
    events: Vec<String>,
    /// ntfy topic name (e.g. "fondaco-a8f2k9x")
    #[serde(default)]
    topic: String,
    /// ntfy server URL (defaults to "https://ntfy.sh")
    #[serde(default)]
    server: String,
    /// Optional per-event priority overrides (e.g. {"error": "urgent"})
    #[serde(default)]
    priority: HashMap<String, String>,
}

impl MediumConfig {
    fn subscribed_to(&self, event: &NotificationEvent) -> bool {
        if !self.enabled {
            return false;
        }
        let event_matches = event.matches();
        let subscribed: HashSet<&str> = self.events.iter().map(|s| s.as_str()).collect();
        event_matches.iter().any(|m| subscribed.contains(m))
    }
}

impl NtfyConfig {
    fn subscribed_to(&self, event: &NotificationEvent) -> bool {
        if !self.enabled || self.topic.is_empty() {
            return false;
        }
        let event_matches = event.matches();
        let subscribed: HashSet<&str> = self.events.iter().map(|s| s.as_str()).collect();
        event_matches.iter().any(|m| subscribed.contains(m))
    }

    fn server_url(&self) -> &str {
        if self.server.is_empty() {
            "https://ntfy.sh"
        } else {
            self.server.trim_end_matches('/')
        }
    }

    fn priority_for(&self, event: &NotificationEvent) -> &str {
        // Check per-event overrides first, then fall back to event default
        for key in event.matches() {
            if let Some(p) = self.priority.get(*key) {
                return p.as_str();
            }
        }
        event.ntfy_priority()
    }
}

// ---------------------------------------------------------------------------
// Manager
// ---------------------------------------------------------------------------

/// Manages sending notifications across all configured mediums.
pub struct NotificationManager {
    config: NotificationsConfig,
}

impl NotificationManager {
    /// Load settings from `settings.json` at the project root.
    /// Falls back to all-disabled if the file is missing or malformed.
    pub fn load() -> Self {
        let path = Self::settings_path();
        let config = match fs::read_to_string(&path) {
            Ok(contents) => match serde_json::from_str::<SettingsFile>(&contents) {
                Ok(sf) => {
                    debug!("Loaded notification settings from {}", path.display());
                    sf.notifications
                }
                Err(e) => {
                    warn!("Failed to parse {}: {} — notifications disabled", path.display(), e);
                    NotificationsConfig::default()
                }
            },
            Err(_) => {
                debug!("No settings.json found — notifications disabled");
                NotificationsConfig::default()
            }
        };
        Self { config }
    }

    /// Construct with explicit config (useful for testing).
    pub fn with_config(desktop: bool, events: Vec<String>) -> Self {
        Self {
            config: NotificationsConfig {
                desktop: MediumConfig {
                    enabled: desktop,
                    events,
                },
                ..Default::default()
            },
        }
    }

    fn settings_path() -> PathBuf {
        crate::defaults::base_dir().join("settings.json")
    }

    /// Send a notification to all subscribed mediums.
    pub fn send(&self, notification: &Notification) {
        if self.config.desktop.subscribed_to(&notification.event) {
            send_desktop_notification(&notification.title, &notification.body);
        }
        if self.config.ntfy.subscribed_to(&notification.event) {
            send_ntfy_notification(&self.config.ntfy, notification);
        }
    }

    /// Convenience: build and send in one call.
    pub fn notify(&self, event: NotificationEvent, title: impl Into<String>, body: impl Into<String>) {
        self.send(&Notification::new(event, title, body));
    }

    /// Check if any medium is listening for this event type.
    pub fn has_subscribers(&self, event: &NotificationEvent) -> bool {
        self.config.desktop.subscribed_to(event)
            || self.config.ntfy.subscribed_to(event)
    }
}

// ---------------------------------------------------------------------------
// Desktop Notifications (notify-rust)
// ---------------------------------------------------------------------------

fn send_desktop_notification(title: &str, body: &str) {
    // Use osascript on macOS — notify-rust delegates to NSUserNotification via the
    // terminal's bundle ID, which is silently suppressed when the terminal app has
    // notifications disabled in System Settings.  osascript always works.
    #[cfg(target_os = "macos")]
    {
        let script = format!(
            "display notification \"{}\" with title \"{}\"",
            body.replace('\\', "\\\\").replace('"', "\\\""),
            title.replace('\\', "\\\\").replace('"', "\\\""),
        );
        if let Err(e) = std::process::Command::new("osascript")
            .args(["-e", &script])
            .output()
        {
            warn!("Desktop notification failed (osascript): {}", e);
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        if let Err(e) = notify_rust::Notification::new()
            .summary(title)
            .body(body)
            .appname("fondaco")
            .show()
        {
            warn!("Desktop notification failed: {}", e);
        }
    }
}

// ---------------------------------------------------------------------------
// ntfy Notifications
// ---------------------------------------------------------------------------

/// Send a push notification via ntfy.sh (or self-hosted ntfy server).
///
/// Simple HTTP POST — no API keys required for the public server.
/// Install the ntfy app on iOS/Android to receive push notifications.
fn send_ntfy_notification(config: &NtfyConfig, notification: &Notification) {
    let url = format!("{}/{}", config.server_url(), config.topic);
    let priority = config.priority_for(&notification.event);
    let tag = notification.event.ntfy_tag();

    let client = match reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            warn!("ntfy: failed to build HTTP client: {}", e);
            return;
        }
    };

    match client
        .post(&url)
        .header("Title", &notification.title)
        .header("Priority", priority)
        .header("Tags", tag)
        .body(notification.body.clone())
        .send()
    {
        Ok(resp) => {
            if !resp.status().is_success() {
                warn!("ntfy: HTTP {} from {}", resp.status(), url);
            }
        }
        Err(e) => {
            warn!("ntfy: failed to send notification: {}", e);
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_event_hierarchy() {
        let config = MediumConfig {
            enabled: true,
            events: vec!["trades".to_string()],
        };
        assert!(config.subscribed_to(&NotificationEvent::TradeBuy));
        assert!(config.subscribed_to(&NotificationEvent::TradeSell));
        assert!(!config.subscribed_to(&NotificationEvent::Error));
    }

    #[test]
    fn test_specific_event() {
        let config = MediumConfig {
            enabled: true,
            events: vec!["trades:buys".to_string()],
        };
        assert!(config.subscribed_to(&NotificationEvent::TradeBuy));
        assert!(!config.subscribed_to(&NotificationEvent::TradeSell));
    }

    #[test]
    fn test_session_hierarchy() {
        let config = MediumConfig {
            enabled: true,
            events: vec!["session:end".to_string()],
        };
        assert!(config.subscribed_to(&NotificationEvent::SessionEndWin));
        assert!(config.subscribed_to(&NotificationEvent::SessionEndLoss));
    }

    #[test]
    fn test_disabled_medium() {
        let config = MediumConfig {
            enabled: false,
            events: vec!["trades".to_string()],
        };
        assert!(!config.subscribed_to(&NotificationEvent::TradeBuy));
    }

    #[test]
    fn test_empty_events() {
        let config = MediumConfig {
            enabled: true,
            events: vec![],
        };
        assert!(!config.subscribed_to(&NotificationEvent::TradeBuy));
        assert!(!config.subscribed_to(&NotificationEvent::Error));
    }

    #[test]
    fn test_manager_no_file() {
        let mgr = NotificationManager::with_config(false, vec![]);
        assert!(!mgr.has_subscribers(&NotificationEvent::TradeBuy));
    }

    #[test]
    fn test_manager_with_config() {
        let mgr = NotificationManager::with_config(true, vec!["trades".to_string(), "error".to_string()]);
        assert!(mgr.has_subscribers(&NotificationEvent::TradeBuy));
        assert!(mgr.has_subscribers(&NotificationEvent::TradeSell));
        assert!(mgr.has_subscribers(&NotificationEvent::Error));
        assert!(!mgr.has_subscribers(&NotificationEvent::ReportHourly));
    }

    #[test]
    fn test_ntfy_config_defaults() {
        let config = NtfyConfig::default();
        assert_eq!(config.server_url(), "https://ntfy.sh");
        assert!(!config.subscribed_to(&NotificationEvent::TradeBuy));
    }

    #[test]
    fn test_ntfy_config_subscribed() {
        let config = NtfyConfig {
            enabled: true,
            topic: "test-topic".to_string(),
            events: vec!["trades".to_string(), "error".to_string()],
            ..Default::default()
        };
        assert!(config.subscribed_to(&NotificationEvent::TradeBuy));
        assert!(config.subscribed_to(&NotificationEvent::Error));
        assert!(!config.subscribed_to(&NotificationEvent::ReportHourly));
    }

    #[test]
    fn test_ntfy_empty_topic_disables() {
        let config = NtfyConfig {
            enabled: true,
            topic: "".to_string(),
            events: vec!["trades".to_string()],
            ..Default::default()
        };
        assert!(!config.subscribed_to(&NotificationEvent::TradeBuy));
    }

    #[test]
    fn test_ntfy_priority_defaults() {
        assert_eq!(NotificationEvent::Error.ntfy_priority(), "urgent");
        assert_eq!(NotificationEvent::SessionEndLoss.ntfy_priority(), "high");
        assert_eq!(NotificationEvent::TradeBuy.ntfy_priority(), "default");
    }

    #[test]
    fn test_ntfy_priority_override() {
        let config = NtfyConfig {
            enabled: true,
            topic: "t".to_string(),
            events: vec!["trades".to_string()],
            priority: HashMap::from([("trades:buys".to_string(), "low".to_string())]),
            ..Default::default()
        };
        // Override takes precedence
        assert_eq!(config.priority_for(&NotificationEvent::TradeBuy), "low");
        // No override, falls back to event default
        assert_eq!(config.priority_for(&NotificationEvent::TradeSell), "default");
    }

    #[test]
    fn test_ntfy_custom_server() {
        let config = NtfyConfig {
            server: "https://ntfy.example.com/".to_string(),
            ..Default::default()
        };
        assert_eq!(config.server_url(), "https://ntfy.example.com");
    }
}
