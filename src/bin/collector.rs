//! Standalone data collector for a single Polymarket up/down market.
//!
//! Ports the Python collector (`python/app/collector.py`) to Rust.
//! Connects to RTDS WebSocket for Chainlink BTC prices, polls CLOB for
//! token midpoints and book tops, and writes session JSON files to disk.
//!
//! Usage:
//!     cargo run --bin collector -- --slug-prefix btc-updown-5m-

use std::collections::VecDeque;
use std::io::Write as IoWrite;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use chrono::Utc;
use clap::Parser;
use log::{debug, error, info, warn};
use reqwest::blocking::Client;

use signal_hook::consts::{SIGUSR1, SIGUSR2};

use eros_pm::clob_ws::ClobBookClient;
use eros_pm::defaults::{API_TIMEOUT_DEFAULT, POLL_INTERVAL_SECONDS};
use eros_pm::engine::load_session_summaries;
use eros_pm::market_discovery::fetch_metadata_with_retry;
use eros_pm::markets::get_market_config;
use eros_pm::models::{MarketMetadata, Sample, SessionData};
use eros_pm::price_fetcher::take_sample;
use eros_pm::rtds::RtdsClient;
use eros_pm::session_cache::SessionStatsCache;
use eros_pm::session_io::{
    clear_live_session, remove_collector_state, remove_file_silent, save_session, session_exists,
    write_collector_state_with_book_source, write_live_session, write_live_status, write_pid_file,
};
use eros_pm::tick_broadcast::{TickBroadcaster, TickMessage};
use eros_pm::window_ops::{predict_next_window, wait_for_next_window};

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

#[derive(Parser)]
#[command(name = "collector", about = "Data collector daemon for a single market")]
struct Cli {
    /// Market slug prefix, e.g. "btc-updown-5m-"
    #[arg(long)]
    slug_prefix: String,

    /// Window duration override in seconds (auto-detected from markets.rs)
    #[arg(long)]
    window: Option<u64>,

    /// Max sessions to collect (0 = unlimited)
    #[arg(long, default_value_t = 0)]
    max_sessions: u64,

    /// Poll interval in seconds (overrides market config and default)
    #[arg(long)]
    poll: Option<f64>,

    /// Output directory (default: "data")
    #[arg(long, default_value = "data")]
    output_dir: String,

    /// Write logs to data/logs/collector-<prefix>.log
    #[arg(long)]
    log_file: bool,

    /// Book data source: "rest" (default, HTTP polling) or "ws" (WebSocket streaming)
    #[arg(long, default_value = "rest")]
    book_source: String,
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

fn run_collector(cli: Cli) {
    let slug_key = cli.slug_prefix.trim_end_matches('-').to_string();

    // Determine window duration and poll interval from CLI > market config > default
    let market_cfg = get_market_config(&cli.slug_prefix);
    let window_duration = if let Some(w) = cli.window {
        w
    } else if let Some(ref cfg) = market_cfg {
        cfg.window_duration_seconds
    } else {
        error!(
            "Unknown market '{}'. Provide --window or add to markets.rs",
            cli.slug_prefix
        );
        std::process::exit(1);
    };

    let poll_interval = cli
        .poll
        .or_else(|| market_cfg.as_ref().and_then(|c| c.poll_interval_seconds))
        .unwrap_or(POLL_INTERVAL_SECONDS);

    info!(
        "[{}] Collector starting. Window: {}s, Poll: {:.3}s, Output: {}, Max sessions: {}",
        slug_key,
        window_duration,
        poll_interval,
        cli.output_dir,
        if cli.max_sessions == 0 {
            "unlimited".to_string()
        } else {
            cli.max_sessions.to_string()
        },
    );

    // Shared shutdown flag (immediate) and stop-after-session flag (graceful)
    let running = Arc::new(AtomicBool::new(true));
    let stop_after_session = Arc::new(AtomicBool::new(false));
    {
        let r = Arc::clone(&running);
        ctrlc::set_handler(move || {
            info!("Shutdown signal received.");
            r.store(false, Ordering::SeqCst);
        })
        .expect("Error setting Ctrl-C handler");
    }
    // SIGUSR1 = finish current session then exit (for graceful restart)
    signal_hook::flag::register(SIGUSR1, Arc::clone(&stop_after_session))
        .expect("Error setting SIGUSR1 handler");
    // SIGUSR2 = toggle book source between REST and WebSocket
    let toggle_book_source = Arc::new(AtomicBool::new(false));
    signal_hook::flag::register(SIGUSR2, Arc::clone(&toggle_book_source))
        .expect("Error setting SIGUSR2 handler");

    // PID file lock — prevent duplicate collectors for the same market
    let pid_path = format!("/tmp/eros-pm-{}-collector.pid", slug_key);
    if let Ok(contents) = std::fs::read_to_string(&pid_path) {
        if let Ok(old_pid) = contents.trim().parse::<u32>() {
            // Check if that process is still alive (signal 0 = existence check)
            let status = std::process::Command::new("kill")
                .args(["-0", &old_pid.to_string()])
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status();
            if status.map(|s| s.success()).unwrap_or(false) {
                error!(
                    "[{}] Another collector is already running (PID {}). Exiting.",
                    slug_key, old_pid
                );
                std::process::exit(1);
            }
        }
    }
    write_pid_file(&pid_path);

    // Load session summaries for stats cache
    let sessions_dir = format!("{}/markets/{}/sessions", cli.output_dir, slug_key);
    let summaries = load_session_summaries(&sessions_dir);
    let mut stats_cache = SessionStatsCache::from_summaries(&summaries);
    info!(
        "[{}] Loaded {} session summaries for stats cache",
        slug_key,
        stats_cache.len()
    );

    // Start tick broadcaster on Unix socket
    let sock_path = format!(
        "{}/markets/{}/tick.sock",
        cli.output_dir, slug_key
    );
    let broadcaster = match TickBroadcaster::new(&sock_path, Arc::clone(&running)) {
        Ok(b) => {
            info!("[{}] Tick broadcaster bound to {}", slug_key, sock_path);
            b
        }
        Err(e) => {
            error!("[{}] Failed to bind tick socket {}: {}", slug_key, sock_path, e);
            std::process::exit(1);
        }
    };

    // Start RTDS client
    let rtds = RtdsClient::new("btc/usd");
    rtds.start();

    // Book source: REST (default) or WebSocket (streaming)
    // SIGUSR2 toggles at runtime. WS client always runs so toggle is instant.
    let use_ws = Arc::new(AtomicBool::new(cli.book_source == "ws"));
    let clob_ws = ClobBookClient::new();
    clob_ws.start();
    info!(
        "[{}] Book source: {} (toggle with SIGUSR2)",
        slug_key,
        if use_ws.load(Ordering::SeqCst) { "ws" } else { "rest" }
    );

    // HTTP client
    let client = Client::builder()
        .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
        .build()
        .expect("Failed to build HTTP client");

    // Last known BTC price (fallback cache)
    let last_btc_price: Arc<Mutex<Option<f64>>> = Arc::new(Mutex::new(None));

    let mut session_count: u64 = 0;
    let mut last_window_ts: Option<u64> = None;

    while running.load(Ordering::SeqCst) {
        // Check for SIGUSR2 toggle
        if toggle_book_source.swap(false, Ordering::SeqCst) {
            let was_ws = use_ws.load(Ordering::SeqCst);
            use_ws.store(!was_ws, Ordering::SeqCst);
            let mode = if !was_ws { "ws" } else { "rest" };
            info!("[{}] Book source toggled to: {}", slug_key, mode);
            write_collector_state_with_book_source(
                &cli.output_dir, &slug_key, session_count, "idle", mode,
            );
        }

        // Write "waiting" status so the dashboard shows the collector as live
        let next_window = predict_next_window(window_duration, last_window_ts);
        let next_window_utc = chrono::DateTime::from_timestamp(next_window as i64, 0)
            .map(|dt| dt.to_rfc3339())
            .unwrap_or_default();
        write_live_status(&cli.output_dir, &slug_key, "waiting", Some(&next_window_utc));

        // Broadcast waiting state to subscribers
        broadcaster.broadcast(&TickMessage::Waiting {
            next_window_utc: next_window_utc.clone(),
        });

        let ws_for_session = if use_ws.load(Ordering::SeqCst) {
            Some(&clob_ws)
        } else {
            None
        };

        match run_one_session(
            &cli.slug_prefix,
            &slug_key,
            window_duration,
            poll_interval,
            &cli.output_dir,
            &client,
            &rtds,
            &last_btc_price,
            &running,
            last_window_ts,
            &broadcaster,
            &mut stats_cache,
            ws_for_session,
        ) {
            Ok(window_ts) => {
                session_count += 1;
                last_window_ts = Some(window_ts);
                let mode = if use_ws.load(Ordering::SeqCst) { "ws" } else { "rest" };
                write_collector_state_with_book_source(
                    &cli.output_dir, &slug_key, session_count, "idle", mode,
                );
                info!(
                    "[{}] Sessions completed: {} (tick clients: {})",
                    slug_key, session_count, broadcaster.client_count()
                );

                if stop_after_session.load(Ordering::SeqCst) {
                    info!(
                        "[{}] SIGUSR1 received — stopping after session (graceful restart).",
                        slug_key
                    );
                    break;
                }

                if cli.max_sessions > 0 && session_count >= cli.max_sessions {
                    info!(
                        "[{}] Reached max sessions ({}). Stopping.",
                        slug_key, cli.max_sessions
                    );
                    break;
                }
            }
            Err(e) => {
                if !running.load(Ordering::SeqCst) {
                    break;
                }
                error!("[{}] Session error: {}", slug_key, e);
                thread::sleep(Duration::from_secs(10));
            }
        }
    }

    rtds.stop();
    clob_ws.stop();
    clear_live_session(&cli.output_dir, &slug_key);
    remove_file_silent(&pid_path);
    remove_collector_state(&cli.output_dir, &slug_key);
    info!(
        "[{}] Collector stopped. {} sessions completed.",
        slug_key, session_count
    );
}

fn run_one_session(
    slug_prefix: &str,
    slug_key: &str,
    window_duration: u64,
    poll_interval: f64,
    output_dir: &str,
    client: &Client,
    rtds: &RtdsClient,
    last_btc_price: &Arc<Mutex<Option<f64>>>,
    running: &AtomicBool,
    last_window_ts: Option<u64>,
    broadcaster: &TickBroadcaster,
    stats_cache: &mut SessionStatsCache,
    clob_ws: Option<&ClobBookClient>,
) -> Result<u64, String> {
    // Pre-fetch metadata and check existence before the window starts
    let target_window = predict_next_window(window_duration, last_window_ts);
    info!("[{}] Pre-fetching metadata for window {}...", slug_key, target_window);
    let metadata = fetch_metadata_with_retry(client, slug_prefix, target_window, running)
        .ok_or_else(|| format!("Could not find market for window {}", target_window))?;

    let window_start_utc = metadata.window_start_utc.as_deref().unwrap_or("");
    if session_exists(output_dir, slug_key, window_start_utc) {
        info!(
            "[{}] Session {} already exists. Skipping.",
            slug_key, window_start_utc
        );
        // Still wait so we don't spin
        let window_ts = wait_for_next_window(window_duration, running, last_window_ts)
            .ok_or_else(|| "shutdown".to_string())?;
        return Ok(window_ts);
    }

    // Wait for the window boundary — polling starts immediately after this
    let window_ts = wait_for_next_window(window_duration, running, last_window_ts)
        .ok_or_else(|| "shutdown".to_string())?;

    if !running.load(Ordering::SeqCst) {
        return Err("shutdown".to_string());
    }

    // If window_ts doesn't match pre-fetched metadata, re-fetch (rare edge case)
    let metadata = if window_ts != target_window {
        warn!(
            "[{}] Window mismatch: expected {}, got {}. Re-fetching metadata.",
            slug_key, target_window, window_ts
        );
        fetch_metadata_with_retry(client, slug_prefix, window_ts, running)
            .ok_or_else(|| format!("Could not find market for window {}", window_ts))?
    } else {
        metadata
    };

    info!(
        "[{}] COLLECTING: {}",
        slug_key,
        metadata.event_title.as_deref().unwrap_or("(unknown)")
    );

    // Subscribe CLOB WS to this session's token IDs (if enabled)
    if let Some(ws) = clob_ws {
        ws.subscribe(
            metadata.up_token_id.as_deref().unwrap_or(""),
            metadata.down_token_id.as_deref().unwrap_or(""),
        );
    }

    let collection_started = Utc::now().to_rfc3339();
    let mut session = SessionData {
        metadata: metadata.clone(),
        initial_btc_price_usd: None,
        final_btc_price_usd: None,
        outcome: None,
        outcome_prices: None,
        samples: Vec::new(),
        collection_started_utc: collection_started,
        collection_ended_utc: String::new(),
        error_count: 0,
        errors: Vec::new(),
        source_file: String::new(),
        _prior_summaries: None,
    };

    clear_live_session(output_dir, slug_key);

    // Compute stats for subscribers
    let window_start_utc_str = metadata.window_start_utc.as_deref().unwrap_or("");
    let prior_summaries = stats_cache.prior(window_start_utc_str, 100);
    let gap_before = stats_cache.gap_before(window_start_utc_str);
    let data_density = stats_cache.data_density(
        window_start_utc_str,
        50,
        Some(window_duration as f64),
    );

    // Broadcast session start to all subscribers
    broadcaster.broadcast(&TickMessage::SessionStart {
        window_ts,
        window_end_ts: window_ts + window_duration,
        window_duration,
        initial_btc_price: None,
        prior_session_summaries: prior_summaries,
        gap_before_seconds: gap_before,
        data_density,
        event_slug: metadata.event_slug.clone(),
        event_title: metadata.event_title.clone(),
        up_token_id: metadata.up_token_id.clone(),
        down_token_id: metadata.down_token_id.clone(),
        window_start_utc: metadata.window_start_utc.clone(),
        window_end_utc: metadata.window_end_utc.clone(),
        condition_id: metadata.condition_id.clone(),
        replayed: false,
    });

    // Poll loop
    poll_samples(
        &mut session,
        &metadata,
        window_ts,
        window_duration,
        poll_interval,
        client,
        rtds,
        last_btc_price,
        running,
        output_dir,
        slug_key,
        broadcaster,
        clob_ws.cloned(),
    );

    // Finalize session
    if let Some(last_sample) = session.samples.last() {
        session.final_btc_price_usd = last_sample.btc_price_usd;
    }
    session.collection_ended_utc = Utc::now().to_rfc3339();

    if let (Some(initial), Some(final_price)) =
        (session.initial_btc_price_usd, session.final_btc_price_usd)
    {
        if final_price > initial {
            session.outcome = Some("Up".to_string());
            session.outcome_prices = Some(vec!["1".to_string(), "0".to_string()]);
        } else {
            session.outcome = Some("Down".to_string());
            session.outcome_prices = Some(vec!["0".to_string(), "1".to_string()]);
        }
    }

    info!(
        "[{}] Window ended. {} samples. BTC: ${:?} -> ${:?}.",
        slug_key,
        session.samples.len(),
        session.initial_btc_price_usd,
        session.final_btc_price_usd,
    );

    clear_live_session(output_dir, slug_key);
    let filepath = save_session(output_dir, slug_key, &session);

    info!(
        "[{}] Saved: {:?} | Outcome: {:?} | Samples: {} | Errors: {}",
        slug_key,
        filepath,
        session.outcome,
        session.samples.len(),
        session.error_count,
    );

    // Broadcast session end to all subscribers
    broadcaster.broadcast(&TickMessage::SessionEnd {
        outcome: session.outcome.clone(),
        window_ts,
        session_filepath: filepath.clone().unwrap_or_default(),
    });

    // Refresh stats cache with the new session
    if session.outcome.is_some() {
        let sessions_dir = format!("{}/markets/{}/sessions", output_dir, slug_key);
        let summaries = load_session_summaries(&sessions_dir);
        *stats_cache = SessionStatsCache::from_summaries(&summaries);
        info!(
            "[{}] Stats cache refreshed: {} entries",
            slug_key,
            stats_cache.len()
        );
    }

    Ok(window_ts)
}

/// Max in-flight samples. Limits memory and thread contention if the API
/// stalls — we stop submitting new work until the oldest future drains.
const MAX_PENDING_SAMPLES: usize = 8;

fn poll_samples(
    session: &mut SessionData,
    metadata: &MarketMetadata,
    window_ts: u64,
    window_duration: u64,
    poll_interval_secs: f64,
    client: &Client,
    rtds: &RtdsClient,
    last_btc_price: &Arc<Mutex<Option<f64>>>,
    running: &AtomicBool,
    output_dir: &str,
    slug_key: &str,
    broadcaster: &TickBroadcaster,
    clob_ws: Option<ClobBookClient>,
) {
    let window_end_ts = window_ts + window_duration;
    let poll_interval = Duration::from_secs_f64(poll_interval_secs);
    let mut next_sample_time = Instant::now();
    let mut last_live_write = Instant::now() - Duration::from_secs(10);

    let up_token_id = metadata.up_token_id.as_deref().unwrap_or("").to_string();
    let down_token_id = metadata.down_token_id.as_deref().unwrap_or("").to_string();
    let window_start_utc = metadata.window_start_utc.as_deref().unwrap_or("").to_string();

    // Pipelined sample collection: submit take_sample to background threads
    // and drain completed futures in submission order (FIFO) so samples are
    // always appended in chronological order regardless of HTTP latency jitter.
    let mut pending: VecDeque<thread::JoinHandle<Result<Sample, String>>> = VecDeque::new();

    while running.load(Ordering::SeqCst) {
        let now_unix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();

        // Hard deadline — stop submitting and drain remaining futures
        if now_unix >= window_end_ts {
            break;
        }

        let now = Instant::now();

        // Submit a new sample if it's time and we haven't hit the pipeline cap
        if now >= next_sample_time && now_unix < window_end_ts && pending.len() < MAX_PENDING_SAMPLES
        {
            let c = client.clone();
            let r = rtds.clone();
            let up = up_token_id.clone();
            let down = down_token_id.clone();
            let wsu = window_start_utc.clone();
            let lbp = Arc::clone(last_btc_price);
            let cws = clob_ws.clone();

            let handle = thread::spawn(move || {
                match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    take_sample(&c, &r, &up, &down, &wsu, &lbp, cws.as_ref())
                })) {
                    Ok(sample) => Ok(sample),
                    Err(_) => Err("sample panic".to_string()),
                }
            });
            pending.push_back(handle);
            next_sample_time += poll_interval;
        }

        // Drain completed futures strictly in submission order — this
        // guarantees samples are appended in the same order they were
        // scheduled, so elapsed_seconds is monotonically non-decreasing.
        while let Some(front) = pending.front() {
            if !front.is_finished() {
                break;
            }
            let handle = pending.pop_front().unwrap();
            match handle.join() {
                Ok(Ok(sample)) => {
                    if session.initial_btc_price_usd.is_none() {
                        session.initial_btc_price_usd = sample.btc_price_usd;
                    }
                    let tick_index = session.samples.len();
                    debug!(
                        "[{}] Sample {}: BTC=${:?} Up={:?} Down={:?}",
                        slug_key,
                        tick_index + 1,
                        sample.btc_price_usd,
                        sample.up_token_midpoint,
                        sample.down_token_midpoint,
                    );
                    // Broadcast tick to subscribers
                    broadcaster.broadcast(&TickMessage::Tick {
                        sample: sample.clone(),
                        tick_index,
                    });
                    session.samples.push(sample);
                }
                Ok(Err(_)) | Err(_) => {
                    session.error_count += 1;
                    if session.errors.len() < 500 {
                        session.errors.push(format!(
                            "{}: sample error",
                            Utc::now().to_rfc3339()
                        ));
                    }
                    warn!("[{}] Sample error", slug_key);
                }
            }
        }

        if now.duration_since(last_live_write) >= Duration::from_secs(2)
            && !session.samples.is_empty()
        {
            write_live_session(output_dir, slug_key, session);
            last_live_write = now;
        }

        // Sleep a short interval to avoid busy-spinning while checking futures
        let sleep_dur = next_sample_time
            .checked_duration_since(Instant::now())
            .unwrap_or(Duration::ZERO)
            .min(Duration::from_millis(10));
        if sleep_dur > Duration::ZERO {
            thread::sleep(sleep_dur);
        }
    }

    // Drain any remaining in-flight samples (window just ended)
    for handle in pending {
        match handle.join() {
            Ok(Ok(sample)) => {
                if session.initial_btc_price_usd.is_none() {
                    session.initial_btc_price_usd = sample.btc_price_usd;
                }
                session.samples.push(sample);
            }
            _ => {
                session.error_count += 1;
            }
        }
    }

    // Sort by server_timestamp if available, falling back to elapsed_seconds.
    // FIFO drain keeps samples nearly ordered, but pipelined I/O with
    // variable latency can cause minor inversions — this final sort
    // guarantees strictly chronological data.
    session.samples.sort_by(|a, b| {
        match (&a.server_timestamp, &b.server_timestamp) {
            (Some(ta), Some(tb)) => ta.cmp(tb),
            _ => a
                .elapsed_seconds
                .partial_cmp(&b.elapsed_seconds)
                .unwrap_or(std::cmp::Ordering::Equal),
        }
    });
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

fn main() {
    let cli = Cli::parse();

    let mut builder =
        env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"));
    builder.format(|buf, record| {
        let ts = chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f");
        writeln!(buf, "{} [{}] {}", ts, record.level(), record.args())
    });

    if cli.log_file {
        let slug_key = cli.slug_prefix.trim_end_matches('-');
        let log_dir = format!("{}/logs", cli.output_dir);
        std::fs::create_dir_all(&log_dir).expect("Failed to create log directory");
        let log_path = format!("{}/collector-{}.log", log_dir, slug_key);
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .unwrap_or_else(|e| panic!("Failed to open log file {}: {}", log_path, e));
        builder.target(env_logger::Target::Pipe(Box::new(file)));
        eprintln!("Logging to {}", log_path);
    }

    builder.init();
    run_collector(cli);
}
