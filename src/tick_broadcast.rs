//! Unix domain socket tick broadcasting between collector and bot workers.
//!
//! The collector runs a `TickBroadcaster` that accepts connections and
//! broadcasts JSON-line messages to all connected clients.  Bot workers
//! connect via `TickSubscriber` and receive a stream of `TickMessage`s.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use log::{debug, info, warn};
use serde::{Deserialize, Serialize};

use crate::models::{Sample, SessionSummary};

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

/// Messages sent from collector to bot workers over the tick socket.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum TickMessage {
    #[serde(rename = "session_start")]
    SessionStart {
        window_ts: u64,
        window_end_ts: u64,
        window_duration: u64,
        initial_btc_price: Option<f64>,
        prior_session_summaries: Vec<SessionSummary>,
        gap_before_seconds: Option<f64>,
        data_density: f64,
        /// Metadata fields
        event_slug: Option<String>,
        event_title: Option<String>,
        up_token_id: Option<String>,
        down_token_id: Option<String>,
        window_start_utc: Option<String>,
        window_end_utc: Option<String>,
        condition_id: Option<String>,
        /// True if this is a replay for a mid-session joiner
        #[serde(default)]
        replayed: bool,
    },
    #[serde(rename = "tick")]
    Tick {
        sample: Sample,
        tick_index: usize,
    },
    #[serde(rename = "session_end")]
    SessionEnd {
        outcome: Option<String>,
        window_ts: u64,
        session_filepath: String,
    },
    #[serde(rename = "waiting")]
    Waiting {
        next_window_utc: String,
    },
}

// ---------------------------------------------------------------------------
// TickBroadcaster (server — runs in collector)
// ---------------------------------------------------------------------------

pub struct TickBroadcaster {
    clients: Arc<Mutex<Vec<UnixStream>>>,
    last_session_start: Arc<Mutex<Option<String>>>,
    #[allow(dead_code)]
    running: Arc<AtomicBool>,
    sock_path: String,
}

impl TickBroadcaster {
    /// Bind to the given Unix socket path and start accepting connections.
    pub fn new(sock_path: &str, running: Arc<AtomicBool>) -> std::io::Result<Self> {
        // Remove stale socket
        let _ = std::fs::remove_file(sock_path);

        // Ensure parent dir exists
        if let Some(parent) = std::path::Path::new(sock_path).parent() {
            let _ = std::fs::create_dir_all(parent);
        }

        let listener = UnixListener::bind(sock_path)?;
        listener.set_nonblocking(true)?;

        let clients: Arc<Mutex<Vec<UnixStream>>> = Arc::new(Mutex::new(Vec::new()));
        let last_session_start: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
        let r = Arc::clone(&running);

        // Acceptor thread
        let cl = Arc::clone(&clients);
        let lss = Arc::clone(&last_session_start);
        thread::spawn(move || {
            while r.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        info!("Tick socket: new client connected");
                        // Accepted sockets inherit non-blocking from listener;
                        // set to blocking so write_all sends complete messages.
                        let _ = stream.set_nonblocking(false);
                        // Lock clients FIRST, then replay + add atomically
                        // so broadcast() can't interleave with the replay write.
                        if let Ok(mut clients) = cl.lock() {
                            if let Ok(ref cached) = lss.lock() {
                                if let Some(ref msg) = **cached {
                                    let mut replay_msg = msg.clone();
                                    if let Ok(mut parsed) =
                                        serde_json::from_str::<TickMessage>(&replay_msg)
                                    {
                                        if let TickMessage::SessionStart {
                                            ref mut replayed, ..
                                        } = parsed
                                        {
                                            *replayed = true;
                                        }
                                        replay_msg =
                                            serde_json::to_string(&parsed).unwrap_or(replay_msg);
                                    }
                                    let _ = writeln!(stream, "{}", replay_msg);
                                }
                            }
                            clients.push(stream);
                        }
                    }
                    Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(100));
                    }
                    Err(e) => {
                        warn!("Tick socket accept error: {}", e);
                        thread::sleep(Duration::from_millis(100));
                    }
                }
            }
        });

        Ok(Self {
            clients,
            last_session_start,
            running,
            sock_path: sock_path.to_string(),
        })
    }

    /// Broadcast a message to all connected clients.
    pub fn broadcast(&self, msg: &TickMessage) {
        let json = match serde_json::to_string(msg) {
            Ok(j) => j,
            Err(e) => {
                warn!("Failed to serialize tick message: {}", e);
                return;
            }
        };

        // Cache session_start for mid-session replay
        if matches!(msg, TickMessage::SessionStart { .. }) {
            if let Ok(mut cached) = self.last_session_start.lock() {
                *cached = Some(json.clone());
            }
        }
        // Clear cache on session end
        if matches!(msg, TickMessage::SessionEnd { .. }) {
            if let Ok(mut cached) = self.last_session_start.lock() {
                *cached = None;
            }
        }

        let line = format!("{}\n", json);

        if let Ok(mut clients) = self.clients.lock() {
            clients.retain_mut(|stream| {
                match stream.write_all(line.as_bytes()) {
                    Ok(_) => true,
                    Err(e) => {
                        debug!("Dropping disconnected tick client: {}", e);
                        false
                    }
                }
            });
        }
    }

    /// Number of connected clients.
    pub fn client_count(&self) -> usize {
        self.clients.lock().map(|c| c.len()).unwrap_or(0)
    }
}

impl Drop for TickBroadcaster {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.sock_path);
    }
}

// ---------------------------------------------------------------------------
// TickSubscriber (client — runs in bot worker)
// ---------------------------------------------------------------------------

pub struct TickSubscriber {
    reader: BufReader<UnixStream>,
}

impl TickSubscriber {
    /// Try to connect to the tick socket, retrying with backoff.
    pub fn connect(sock_path: &str, running: &AtomicBool) -> Option<Self> {
        let mut delay_ms = 100u64;
        let max_delay_ms = 5000u64;

        while running.load(Ordering::SeqCst) {
            match UnixStream::connect(sock_path) {
                Ok(stream) => {
                    info!("Connected to tick socket: {}", sock_path);
                    return Some(Self {
                        reader: BufReader::with_capacity(256 * 1024, stream),
                    });
                }
                Err(e) => {
                    debug!("Tick socket connect failed ({}), retrying in {}ms", e, delay_ms);
                    thread::sleep(Duration::from_millis(delay_ms));
                    delay_ms = (delay_ms * 2).min(max_delay_ms);
                }
            }
        }
        None
    }

    /// Read the next message (blocking).
    pub fn next_message(&mut self) -> Option<TickMessage> {
        let mut line = String::new();
        match self.reader.read_line(&mut line) {
            Ok(0) => None, // EOF — server disconnected
            Ok(_) => {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    return self.next_message(); // skip blank lines
                }
                match serde_json::from_str(trimmed) {
                    Ok(msg) => Some(msg),
                    Err(e) => {
                        warn!("Failed to parse tick message: {} — line: {}", e, trimmed);
                        self.next_message() // skip bad lines
                    }
                }
            }
            Err(e) => {
                warn!("Tick socket read error: {}", e);
                None
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;

    #[test]
    fn test_tick_message_roundtrip() {
        let msg = TickMessage::Tick {
            sample: Sample {
                timestamp_utc: Some("2026-01-01T00:00:00Z".to_string()),
                elapsed_seconds: 1.5,
                btc_price_usd: Some(60000.0),
                up_token_midpoint: Some(0.55),
                down_token_midpoint: Some(0.45),
                up_token_best_bid: Some(0.01),
                up_token_best_ask: Some(0.99),
                down_token_best_bid: Some(0.01),
                down_token_best_ask: Some(0.99),
                server_timestamp: None,
            },
            tick_index: 42,
        };
        let json = serde_json::to_string(&msg).unwrap();
        let parsed: TickMessage = serde_json::from_str(&json).unwrap();
        match parsed {
            TickMessage::Tick { tick_index, .. } => assert_eq!(tick_index, 42),
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn test_session_start_roundtrip() {
        let msg = TickMessage::SessionStart {
            window_ts: 1000,
            window_end_ts: 1300,
            window_duration: 300,
            initial_btc_price: Some(60000.0),
            prior_session_summaries: vec![],
            gap_before_seconds: None,
            data_density: 1.0,
            event_slug: Some("btc-updown-5m-123".to_string()),
            event_title: Some("BTC Up/Down".to_string()),
            up_token_id: Some("t1".to_string()),
            down_token_id: Some("t2".to_string()),
            window_start_utc: Some("2026-01-01T00:00:00Z".to_string()),
            window_end_utc: Some("2026-01-01T00:05:00Z".to_string()),
            condition_id: Some("c1".to_string()),
            replayed: false,
        };
        let json = serde_json::to_string(&msg).unwrap();
        let parsed: TickMessage = serde_json::from_str(&json).unwrap();
        match parsed {
            TickMessage::SessionStart { window_ts, replayed, .. } => {
                assert_eq!(window_ts, 1000);
                assert!(!replayed);
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn test_broadcaster_subscriber() {
        let running = Arc::new(AtomicBool::new(true));
        let sock_path = format!("/tmp/eros-pm-test-tick-{}.sock", std::process::id());

        let broadcaster = TickBroadcaster::new(&sock_path, Arc::clone(&running)).unwrap();
        assert_eq!(broadcaster.client_count(), 0);

        // Connect subscriber
        let mut sub = TickSubscriber::connect(&sock_path, &running).unwrap();
        thread::sleep(Duration::from_millis(200)); // let acceptor run
        assert_eq!(broadcaster.client_count(), 1);

        // Broadcast a tick
        let msg = TickMessage::Waiting {
            next_window_utc: "2026-01-01T00:05:00Z".to_string(),
        };
        broadcaster.broadcast(&msg);

        let received = sub.next_message().unwrap();
        match received {
            TickMessage::Waiting { next_window_utc } => {
                assert_eq!(next_window_utc, "2026-01-01T00:05:00Z");
            }
            _ => panic!("wrong variant"),
        }

        running.store(false, Ordering::SeqCst);
        drop(broadcaster);
    }
}
