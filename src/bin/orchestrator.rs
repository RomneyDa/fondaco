//! Orchestrator binary — manages clob-bridge, collectors, and bot workers.
//!
//! Provides an HTTP control API on port 8420 that the dashboard proxies to.
//! Stateless: all process discovery is done via PID files in /tmp.
//!
//! Usage:
//!     cargo run --bin orchestrator

use std::collections::HashMap;
use std::fs;
use std::io::Write as IoWrite;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use chrono::Utc;
use clap::Parser;
use log::{error, info, warn};
use serde_json::{json, Value};
use tiny_http::{Header, Method, Response, Server};

use eros_pm::notifications::{NotificationEvent, NotificationManager};

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

#[derive(Parser)]
#[command(name = "orchestrator", about = "Manages clob-bridge, collectors, and bot workers")]
struct Cli {
    /// HTTP control port
    #[arg(long, default_value_t = 8420)]
    port: u16,

    /// CLOB bridge port
    #[arg(long, default_value_t = 8421)]
    clob_bridge_port: u16,

    /// Data directory
    #[arg(long, default_value = "data")]
    data_dir: String,

    /// Skip starting clob-bridge (assume it's managed externally)
    #[arg(long)]
    no_clob_bridge: bool,
}

// ---------------------------------------------------------------------------
// Orchestrator state (minimal — only clob-bridge handle + config)
// ---------------------------------------------------------------------------

struct OrchestratorState {
    clob_bridge: Option<Child>,
    clob_bridge_port: u16,
    data_dir: String,
    notifier: NotificationManager,
}

impl OrchestratorState {
    fn new(data_dir: &str, clob_bridge_port: u16) -> Self {
        Self {
            clob_bridge: None,
            clob_bridge_port,
            data_dir: data_dir.to_string(),
            notifier: NotificationManager::load(),
        }
    }

    fn clob_bridge_healthy(&self) -> bool {
        let url = format!("http://127.0.0.1:{}/health", self.clob_bridge_port);
        reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(3))
            .build()
            .ok()
            .and_then(|c| c.get(&url).send().ok())
            .map(|r| r.status().is_success())
            .unwrap_or(false)
    }
}

// ---------------------------------------------------------------------------
// Process discovery — all from PID files, no internal state
// ---------------------------------------------------------------------------

mod libc {
    pub const WNOHANG: i32 = 1;
    extern "C" {
        pub fn kill(pid: i32, sig: i32) -> i32;
        pub fn waitpid(pid: i32, status: *mut i32, options: i32) -> i32;
    }
}

fn pid_alive(pid: u32) -> bool {
    unsafe {
        libc::waitpid(pid as i32, std::ptr::null_mut(), libc::WNOHANG);
    }
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

fn read_pid_file(path: &str) -> Option<u32> {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| s.trim().parse().ok())
}

fn read_pid_file_alive(path: &str) -> Option<u32> {
    let pid = read_pid_file(path)?;
    if pid_alive(pid) {
        Some(pid)
    } else {
        let _ = fs::remove_file(path);
        None
    }
}

/// Get collector PID for a market from its PID file.
fn collector_pid(slug_key: &str) -> Option<u32> {
    read_pid_file_alive(&format!("/tmp/eros-pm-{}-collector.pid", slug_key))
}

/// Get worker PID for a strategy from its PID file.
fn worker_pid(slug_key: &str, strategy: &str) -> Option<u32> {
    read_pid_file_alive(&format!("/tmp/eros-pm-{}-bot-{}.pid", slug_key, strategy))
}

/// Discover all live bot-worker PIDs for a market from PID files.
fn discover_workers(slug_key: &str) -> Vec<(String, u32)> {
    let prefix = format!("eros-pm-{}-bot-", slug_key);
    let mut results = Vec::new();
    if let Ok(dir) = fs::read_dir("/tmp") {
        for f in dir.flatten() {
            let name = f.file_name().to_string_lossy().to_string();
            if name.starts_with(&prefix) && name.ends_with(".pid") {
                let strategy = name
                    .strip_prefix(&prefix)
                    .and_then(|s| s.strip_suffix(".pid"))
                    .unwrap_or("")
                    .to_string();
                if strategy.is_empty() {
                    continue;
                }
                if let Some(pid) = read_pid_file_alive(&f.path().to_string_lossy()) {
                    results.push((strategy, pid));
                }
            }
        }
    }
    results
}

/// Read a worker's command-line args from `ps`.
fn read_process_args(pid: u32) -> Option<String> {
    Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "args="])
        .output()
        .ok()
        .and_then(|o| {
            let s = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if s.is_empty() || s.contains("<defunct>") { None } else { Some(s) }
        })
}

/// Parse --session-cash-ratio from process args string.
fn parse_ratio_from_args(args: &str) -> Option<String> {
    let parts: Vec<&str> = args.split_whitespace().collect();
    for (i, p) in parts.iter().enumerate() {
        if *p == "--session-cash-ratio" {
            return parts.get(i + 1).map(|s| s.to_string());
        }
    }
    None
}

/// Parse --cash from process args string.
fn parse_cash_from_args(args: &str) -> Option<f64> {
    let parts: Vec<&str> = args.split_whitespace().collect();
    for (i, p) in parts.iter().enumerate() {
        if *p == "--cash" {
            return parts.get(i + 1).and_then(|s| s.parse().ok());
        }
    }
    None
}

/// Check if --dry-run is in process args.
fn parse_dry_run_from_args(args: &str) -> bool {
    args.split_whitespace().any(|p| p == "--dry-run")
}

/// Build status JSON by discovering live state from PID files.
fn build_status(st: &OrchestratorState) -> Value {
    let mut markets: HashMap<String, Value> = HashMap::new();

    let markets_dir = PathBuf::from(&st.data_dir).join("markets");
    if let Ok(entries) = fs::read_dir(&markets_dir) {
        for entry in entries.flatten() {
            let slug_key = entry.file_name().to_string_lossy().to_string();
            if !entry.path().join("sessions").is_dir() {
                continue;
            }

            let market = markets.entry(slug_key.clone()).or_insert_with(|| json!({}));

            // Collector
            if let Some(pid) = collector_pid(&slug_key) {
                market["collector"] = json!({ "pid": pid, "alive": true });
            }

            // Workers
            let workers = discover_workers(&slug_key);
            if !workers.is_empty() {
                let mut workers_json: Vec<Value> = Vec::new();
                for (strategy, pid) in &workers {
                    let mut info = json!({
                        "pid": pid,
                        "strategy": strategy,
                        "alive": true,
                    });
                    // Enrich from process args
                    if let Some(args) = read_process_args(*pid) {
                        if let Some(cash) = parse_cash_from_args(&args) {
                            info["cash"] = json!(cash);
                        }
                        if let Some(ratio) = parse_ratio_from_args(&args) {
                            if let Some((r, m)) = ratio.split_once(':') {
                                if let (Ok(r), Ok(m)) = (r.parse::<f64>(), m.parse::<f64>()) {
                                    info["session_cash_ratio"] = json!({ "ratio": r, "max": m });
                                }
                            }
                        }
                        info["dry_run"] = json!(parse_dry_run_from_args(&args));
                    }
                    workers_json.push(info);
                }
                market["workers"] = json!(workers_json);
            }
        }
    }

    json!({
        "clob_bridge": {
            "port": st.clob_bridge_port,
            "healthy": st.clob_bridge_healthy(),
            "managed": st.clob_bridge.is_some(),
        },
        "markets": markets,
    })
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

fn json_response(status: u16, body: &Value) -> Response<std::io::Cursor<Vec<u8>>> {
    let body_str = serde_json::to_string(body).unwrap_or_default();
    let header = Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]).unwrap();
    Response::from_data(body_str.into_bytes())
        .with_status_code(status)
        .with_header(header)
}

fn read_body(req: &mut tiny_http::Request) -> Value {
    let mut body = String::new();
    let _ = req.as_reader().read_to_string(&mut body);
    serde_json::from_str(&body).unwrap_or(json!({}))
}

fn find_binary(name: &str) -> PathBuf {
    let release = PathBuf::from(format!("target/release/{}", name));
    if release.exists() {
        return release;
    }
    PathBuf::from(format!("target/debug/{}", name))
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .format(|buf, record| {
            let ts = chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f");
            writeln!(buf, "{} [{}] {}", ts, record.level(), record.args())
        })
        .init();

    let cli = Cli::parse();
    let port = cli.port;

    // Write app.json so the dashboard can find us
    let app_json_path = PathBuf::from(&cli.data_dir).join("app.json");
    let _ = fs::create_dir_all(&cli.data_dir);
    let app_data = json!({
        "port": port,
        "pid": std::process::id(),
        "started_at": Utc::now().to_rfc3339(),
    });
    if let Ok(mut f) = fs::File::create(&app_json_path) {
        let _ = serde_json::to_writer_pretty(&mut f, &app_data);
    }
    info!("Wrote {}", app_json_path.display());

    let state = Arc::new(Mutex::new(OrchestratorState::new(&cli.data_dir, cli.clob_bridge_port)));

    // Start clob-bridge if not disabled
    if !cli.no_clob_bridge {
        info!("Starting clob-bridge on port {}...", cli.clob_bridge_port);
        let bridge_dir = PathBuf::from("clob-bridge");
        if bridge_dir.exists() {
            match Command::new("uv")
                .args(["run", "python", "main.py", "--port", &cli.clob_bridge_port.to_string()])
                .current_dir(&bridge_dir)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
            {
                Ok(child) => {
                    let pid = child.id();
                    info!("clob-bridge started (PID {})", pid);
                    state.lock().unwrap().clob_bridge = Some(child);
                }
                Err(e) => {
                    warn!("Failed to start clob-bridge: {}. Continuing without it.", e);
                }
            }
        } else {
            warn!("clob-bridge/ directory not found. Skipping.");
        }

        // Give it a moment to start
        thread::sleep(Duration::from_secs(2));
    }

    let running = Arc::new(AtomicBool::new(true));
    {
        let r = Arc::clone(&running);
        ctrlc::set_handler(move || {
            info!("Shutdown signal received.");
            r.store(false, Ordering::SeqCst);
        })
        .expect("Error setting Ctrl-C handler");
    }

    // Start HTTP server
    let addr = format!("0.0.0.0:{}", port);
    let server = match Server::http(&addr) {
        Ok(s) => {
            info!("Orchestrator listening on {}", addr);
            s
        }
        Err(e) => {
            error!("Failed to bind HTTP server on {}: {}", addr, e);
            std::process::exit(1);
        }
    };

    while running.load(Ordering::SeqCst) {
        let req = match server.recv_timeout(Duration::from_secs(1)) {
            Ok(Some(r)) => r,
            Ok(None) => continue,
            Err(e) => {
                warn!("HTTP recv error: {}", e);
                continue;
            }
        };

        let method = req.method().clone();
        let url = req.url().to_string();
        handle_request(req, &method, &url, &state);
    }

    // Cleanup — orchestrator exits, children keep running
    info!("Orchestrator exiting (children left running).");
    let _ = fs::remove_file(&app_json_path);
    info!("Orchestrator stopped.");
}

fn handle_request(
    mut req: tiny_http::Request,
    method: &Method,
    url: &str,
    state: &Arc<Mutex<OrchestratorState>>,
) {
    let resp = match (method, url) {
        (&Method::Get, "/status") => {
            let st = state.lock().unwrap();
            json_response(200, &build_status(&st))
        }

        (&Method::Post, "/market-on") => {
            let body = read_body(&mut req);
            let slug_prefix = body["slug_prefix"].as_str().unwrap_or("");
            if slug_prefix.is_empty() {
                json_response(400, &json!({"error": "Missing slug_prefix"}))
            } else {
                let slug_key = slug_prefix.trim_end_matches('-');

                // Check PID file
                if let Some(pid) = collector_pid(slug_key) {
                    let _ = req.respond(json_response(200, &json!({"ok": true, "already_running": true, "pid": pid})));
                    return;
                }

                let bin = find_binary("collector");
                if !bin.exists() {
                    let _ = req.respond(json_response(500, &json!({"error": "collector binary not found"})));
                    return;
                }

                let st = state.lock().unwrap();
                let mut args = vec!["--slug-prefix".to_string(), slug_prefix.to_string()];
                args.push("--output-dir".to_string());
                args.push(st.data_dir.clone());
                if let Some(w) = body["window"].as_u64() {
                    args.push("--window".to_string());
                    args.push(w.to_string());
                }

                match Command::new(&bin)
                    .args(&args)
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .spawn()
                {
                    Ok(child) => {
                        let pid = child.id();
                        info!("Started collector for {} (PID {})", slug_key, pid);
                        st.notifier.notify(
                            NotificationEvent::ProcessStart,
                            "Collector Started",
                            format!("{} (PID {})", slug_key, pid),
                        );
                        json_response(200, &json!({"ok": true, "pid": pid}))
                    }
                    Err(e) => {
                        json_response(500, &json!({"error": format!("Failed to start collector: {}", e)}))
                    }
                }
            }
        }

        (&Method::Post, "/market-off") => {
            let body = read_body(&mut req);
            let slug_prefix = body["slug_prefix"].as_str().unwrap_or("");
            if slug_prefix.is_empty() {
                json_response(400, &json!({"error": "Missing slug_prefix"}))
            } else {
                let slug_key = slug_prefix.trim_end_matches('-');
                let after_session = body["after_session"].as_bool().unwrap_or(false);

                if let Some(pid) = collector_pid(slug_key) {
                    let sig = if after_session { 10 } else { 15 }; // SIGUSR1 or SIGTERM
                    unsafe { libc::kill(pid as i32, sig); }
                    info!("Sent signal {} to collector {} (PID {})", sig, slug_key, pid);
                    let st = state.lock().unwrap();
                    st.notifier.notify(
                        NotificationEvent::ProcessStop,
                        "Collector Stopping",
                        format!("{} (PID {})", slug_key, pid),
                    );
                    json_response(200, &json!({"ok": true, "pid": pid, "signal": sig}))
                } else {
                    json_response(200, &json!({"ok": false, "error": "No collector running"}))
                }
            }
        }

        (&Method::Post, "/trade-on") => {
            let body = read_body(&mut req);
            let slug_prefix = body["slug_prefix"].as_str().unwrap_or("");
            let strategy = body["strategy"].as_str().unwrap_or("");
            if slug_prefix.is_empty() || strategy.is_empty() {
                json_response(400, &json!({"error": "Missing slug_prefix or strategy"}))
            } else {
                let slug_key = slug_prefix.trim_end_matches('-');

                // Check if already running
                if let Some(pid) = worker_pid(slug_key, strategy) {
                    let _ = req.respond(json_response(200, &json!({
                        "ok": true,
                        "already_running": true,
                        "pid": pid
                    })));
                    return;
                }

                let st = state.lock().unwrap();

                // Validation: check clob-bridge
                if !st.clob_bridge_healthy() {
                    let _ = req.respond(json_response(503, &json!({"error": "clob-bridge not healthy"})));
                    return;
                }

                // Validation: check collector running
                if collector_pid(slug_key).is_none() {
                    let _ = req.respond(json_response(503, &json!({"error": "No collector running for this market"})));
                    return;
                }

                // Validation: check tick socket
                let sock_path = format!("{}/markets/{}/tick.sock", st.data_dir, slug_key);
                if !Path::new(&sock_path).exists() {
                    let _ = req.respond(json_response(503, &json!({"error": "Tick socket not found"})));
                    return;
                }

                let bin = find_binary("bot-worker");
                if !bin.exists() {
                    let _ = req.respond(json_response(500, &json!({"error": "bot-worker binary not found"})));
                    return;
                }

                let mut args = vec![
                    "--slug-prefix".to_string(), slug_prefix.to_string(),
                    "--strategy".to_string(), strategy.to_string(),
                    "--data-dir".to_string(), st.data_dir.clone(),
                    "--clob-bridge".to_string(), format!("http://127.0.0.1:{}", st.clob_bridge_port),
                ];
                if let Some(cash) = body["cash"].as_f64() {
                    args.push("--cash".to_string());
                    args.push(cash.to_string());
                }
                // Accept either string "0.08:30" or object { ratio: 0.08, max: 30 }
                let ratio_str = if let Some(s) = body["session_cash_ratio"].as_str() {
                    Some(s.to_string())
                } else if body["session_cash_ratio"].is_object() {
                    let r = body["session_cash_ratio"]["ratio"].as_f64();
                    let m = body["session_cash_ratio"]["max"].as_f64();
                    if let (Some(r), Some(m)) = (r, m) {
                        Some(format!("{}:{}", r, m))
                    } else {
                        None
                    }
                } else {
                    None
                };
                if let Some(ref ratio) = ratio_str {
                    args.push("--session-cash-ratio".to_string());
                    args.push(ratio.clone());
                }
                if body["dry_run"].as_bool().unwrap_or(false) {
                    args.push("--dry-run".to_string());
                }

                match Command::new(&bin)
                    .args(&args)
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .spawn()
                {
                    Ok(child) => {
                        let pid = child.id();
                        info!("Started bot-worker {} for {} (PID {})", strategy, slug_key, pid);
                        st.notifier.notify(
                            NotificationEvent::ProcessStart,
                            "Bot Worker Started",
                            format!("{} on {} (PID {})", strategy, slug_key, pid),
                        );
                        json_response(200, &json!({"ok": true, "pid": pid}))
                    }
                    Err(e) => {
                        json_response(500, &json!({"error": format!("Failed to start bot-worker: {}", e)}))
                    }
                }
            }
        }

        (&Method::Post, "/trade-off") => {
            let body = read_body(&mut req);
            let slug_prefix = body["slug_prefix"].as_str().unwrap_or("");
            let strategy = body["strategy"].as_str().unwrap_or("");
            let target_pid = body["pid"].as_u64().map(|p| p as u32);

            let slug_key = slug_prefix.trim_end_matches('-');
            let mut stopped = 0;

            if let Some(pid) = target_pid {
                // Kill specific PID
                if pid_alive(pid) {
                    unsafe { libc::kill(pid as i32, 15); }
                    info!("Stopping worker PID {}", pid);
                    stopped += 1;
                }
            } else if !strategy.is_empty() {
                // Kill specific strategy
                if let Some(pid) = worker_pid(slug_key, strategy) {
                    unsafe { libc::kill(pid as i32, 15); }
                    info!("Stopping worker {} (PID {})", strategy, pid);
                    stopped += 1;
                }
            } else {
                // Kill all workers for this market
                for (strat, pid) in discover_workers(slug_key) {
                    unsafe { libc::kill(pid as i32, 15); }
                    info!("Stopping worker {} (PID {})", strat, pid);
                    stopped += 1;
                }
            }

            json_response(200, &json!({"ok": true, "stopped": stopped}))
        }

        (&Method::Post, "/set-cash") => {
            let body = read_body(&mut req);
            let slug_prefix = body["slug_prefix"].as_str().unwrap_or("");
            let strategy = body["strategy"].as_str().unwrap_or("");
            let cash = body["cash"].as_f64().unwrap_or(0.0);

            if slug_prefix.is_empty() || strategy.is_empty() || cash <= 0.0 {
                json_response(400, &json!({"error": "Missing slug_prefix, strategy, or cash"}))
            } else {
                let st = state.lock().unwrap();
                let slug_key = slug_prefix.trim_end_matches('-');
                let state_file = format!("{}/markets/{}/bot_positions/{}.json", st.data_dir, slug_key, strategy);
                match fs::read_to_string(&state_file) {
                    Ok(contents) => {
                        if let Ok(mut data) = serde_json::from_str::<Value>(&contents) {
                            data["pending_cash"] = json!(cash);
                            if let Ok(mut f) = fs::File::create(&state_file) {
                                let _ = serde_json::to_writer_pretty(&mut f, &data);
                            }
                            json_response(200, &json!({"ok": true, "cash": cash}))
                        } else {
                            json_response(500, &json!({"error": "Failed to parse state file"}))
                        }
                    }
                    Err(_) => json_response(404, &json!({"error": "Worker state file not found"})),
                }
            }
        }

        (&Method::Post, "/reload") => {
            let body = read_body(&mut req);
            let slug_prefix = body["slug_prefix"].as_str().unwrap_or("");
            let strategy = body["strategy"].as_str().unwrap_or("");

            if slug_prefix.is_empty() || strategy.is_empty() {
                json_response(400, &json!({"error": "Missing slug_prefix or strategy"}))
            } else {
                let st = state.lock().unwrap();
                let slug_key = slug_prefix.trim_end_matches('-');

                // Find and read config from running process before killing
                let old_pid = worker_pid(slug_key, strategy);
                let old_args = old_pid.and_then(read_process_args);
                let old_ratio = old_args.as_ref().and_then(|a| parse_ratio_from_args(a));
                let old_dry_run = old_args.as_ref().map(|a| parse_dry_run_from_args(a)).unwrap_or(false);

                if let Some(pid) = old_pid {
                    unsafe { libc::kill(pid as i32, 15); }
                    info!("Killing worker {} (PID {}) for reload", strategy, pid);
                } else {
                    let _ = req.respond(json_response(404, &json!({"error": "Worker not found"})));
                    return;
                }

                // Brief delay, then respawn
                thread::sleep(Duration::from_millis(500));
                let bin = find_binary("bot-worker");

                // Read bankroll from state file
                let state_file = format!("{}/markets/{}/bot_positions/{}.json", st.data_dir, slug_key, strategy);
                let cash = fs::read_to_string(&state_file)
                    .ok()
                    .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                    .and_then(|v| v["pending_cash"].as_f64().or(v["bankroll"].as_f64()))
                    .unwrap_or(100.0);

                let mut args = vec![
                    "--slug-prefix".to_string(), slug_prefix.to_string(),
                    "--strategy".to_string(), strategy.to_string(),
                    "--data-dir".to_string(), st.data_dir.clone(),
                    "--clob-bridge".to_string(), format!("http://127.0.0.1:{}", st.clob_bridge_port),
                    "--cash".to_string(), cash.to_string(),
                ];
                if let Some(ref ratio) = old_ratio {
                    args.push("--session-cash-ratio".to_string());
                    args.push(ratio.clone());
                }
                if old_dry_run {
                    args.push("--dry-run".to_string());
                }

                match Command::new(&bin).args(&args).stdout(Stdio::null()).stderr(Stdio::null()).spawn() {
                    Ok(child) => {
                        let pid = child.id();
                        json_response(200, &json!({"ok": true, "pid": pid, "cash": cash}))
                    }
                    Err(e) => json_response(500, &json!({"error": format!("Respawn failed: {}", e)})),
                }
            }
        }

        (&Method::Post, "/stop") => {
            let mut st = state.lock().unwrap();
            info!("Stop requested — killing workers + clob-bridge, saving state for restore");

            // Save running worker configs for restore, then kill them
            let markets_dir = PathBuf::from(&st.data_dir).join("markets");
            let mut saved_workers: Vec<Value> = Vec::new();
            if let Ok(entries) = fs::read_dir(&markets_dir) {
                for entry in entries.flatten() {
                    let slug_key = entry.file_name().to_string_lossy().to_string();
                    for (strategy, pid) in discover_workers(&slug_key) {
                        if let Some(args) = read_process_args(pid) {
                            let mut w = json!({
                                "slug_prefix": format!("{}-", slug_key),
                                "strategy": strategy,
                            });
                            if let Some(cash) = parse_cash_from_args(&args) {
                                w["cash"] = json!(cash);
                            }
                            if let Some(ratio) = parse_ratio_from_args(&args) {
                                w["session_cash_ratio"] = json!(ratio);
                            }
                            if parse_dry_run_from_args(&args) {
                                w["dry_run"] = json!(true);
                            }
                            saved_workers.push(w);
                        }
                        unsafe { libc::kill(pid as i32, 15); }
                        info!("Stopped worker {} (PID {})", strategy, pid);
                    }
                }
            }
            // Write restore file
            let restore_path = PathBuf::from(&st.data_dir).join("restore_workers.json");
            if !saved_workers.is_empty() {
                if let Ok(mut f) = fs::File::create(&restore_path) {
                    let _ = serde_json::to_writer_pretty(&mut f, &json!(saved_workers));
                }
                info!("Saved {} workers to restore_workers.json", saved_workers.len());
            }

            // Kill clob-bridge
            if let Some(ref mut child) = st.clob_bridge {
                let _ = child.kill();
            }
            st.clob_bridge = None;

            st.notifier.notify(
                NotificationEvent::ProcessStop,
                "Orchestrator Stopped",
                "Orchestrator exiting (workers saved for restore)".to_string(),
            );
            let resp = json_response(200, &json!({"ok": true}));
            let _ = req.respond(resp);
            let app_json = format!("{}/app.json", st.data_dir);
            let _ = fs::remove_file(&app_json);
            std::process::exit(0);
        }

        (&Method::Post, "/start") => {
            let body = read_body(&mut req);
            let restore = body["restore"].as_bool().unwrap_or(false);
            let st = state.lock().unwrap();

            if !restore {
                let _ = req.respond(json_response(200, &json!({"ok": true})));
                return;
            }

            // Restore workers from saved state
            let restore_path = PathBuf::from(&st.data_dir).join("restore_workers.json");
            let workers: Vec<Value> = fs::read_to_string(&restore_path)
                .ok()
                .and_then(|s| serde_json::from_str(&s).ok())
                .unwrap_or_default();

            if workers.is_empty() {
                let _ = req.respond(json_response(200, &json!({"ok": true, "restored": 0})));
                return;
            }

            let bin = find_binary("bot-worker");
            let mut restored = 0;
            for w in &workers {
                let slug_prefix = w["slug_prefix"].as_str().unwrap_or("");
                let strategy = w["strategy"].as_str().unwrap_or("");
                if slug_prefix.is_empty() || strategy.is_empty() {
                    continue;
                }
                let slug_key = slug_prefix.trim_end_matches('-');

                // Only restore if collector is running
                if collector_pid(slug_key).is_none() {
                    info!("Skipping restore of {} — no collector for {}", strategy, slug_key);
                    continue;
                }

                let mut args = vec![
                    "--slug-prefix".to_string(), slug_prefix.to_string(),
                    "--strategy".to_string(), strategy.to_string(),
                    "--data-dir".to_string(), st.data_dir.clone(),
                    "--clob-bridge".to_string(), format!("http://127.0.0.1:{}", st.clob_bridge_port),
                ];
                if let Some(cash) = w["cash"].as_f64() {
                    args.push("--cash".to_string());
                    args.push(cash.to_string());
                }
                if let Some(ratio) = w["session_cash_ratio"].as_str() {
                    args.push("--session-cash-ratio".to_string());
                    args.push(ratio.to_string());
                }
                if w["dry_run"].as_bool().unwrap_or(false) {
                    args.push("--dry-run".to_string());
                }

                match Command::new(&bin).args(&args).stdout(Stdio::null()).stderr(Stdio::null()).spawn() {
                    Ok(child) => {
                        info!("Restored worker {} for {} (PID {})", strategy, slug_key, child.id());
                        restored += 1;
                    }
                    Err(e) => {
                        warn!("Failed to restore worker {} for {}: {}", strategy, slug_key, e);
                    }
                }
            }

            // Clean up restore file
            let _ = fs::remove_file(&restore_path);

            json_response(200, &json!({"ok": true, "restored": restored}))
        }

        (&Method::Post, "/stop-all") => {
            let mut st = state.lock().unwrap();
            // Kill all workers and collectors discovered from PID files
            let markets_dir = PathBuf::from(&st.data_dir).join("markets");
            if let Ok(entries) = fs::read_dir(&markets_dir) {
                for entry in entries.flatten() {
                    let slug_key = entry.file_name().to_string_lossy().to_string();
                    if let Some(pid) = collector_pid(&slug_key) {
                        unsafe { libc::kill(pid as i32, 15); }
                    }
                    for (_, pid) in discover_workers(&slug_key) {
                        unsafe { libc::kill(pid as i32, 15); }
                    }
                }
            }
            // Kill clob-bridge if managed
            if let Some(ref mut child) = st.clob_bridge {
                let _ = child.kill();
            }
            st.clob_bridge = None;
            info!("Stop-all requested — exiting");
            st.notifier.notify(
                NotificationEvent::ProcessStop,
                "Orchestrator Shutdown",
                "Orchestrator shutting down (all children stopped)".to_string(),
            );
            let resp = json_response(200, &json!({"ok": true}));
            let _ = req.respond(resp);
            let app_json = format!("{}/app.json", st.data_dir);
            let _ = fs::remove_file(&app_json);
            std::process::exit(0);
        }

        (&Method::Post, "/kill-all") => {
            let mut st = state.lock().unwrap();
            let mut killed = 0;
            let markets_dir = PathBuf::from(&st.data_dir).join("markets");
            if let Ok(entries) = fs::read_dir(&markets_dir) {
                for entry in entries.flatten() {
                    let slug_key = entry.file_name().to_string_lossy().to_string();
                    if let Some(pid) = collector_pid(&slug_key) {
                        unsafe { libc::kill(pid as i32, 9); }
                        killed += 1;
                    }
                    for (_, pid) in discover_workers(&slug_key) {
                        unsafe { libc::kill(pid as i32, 9); }
                        killed += 1;
                    }
                }
            }
            if let Some(ref mut child) = st.clob_bridge {
                let _ = child.kill();
                killed += 1;
            }
            st.clob_bridge = None;
            json_response(200, &json!({"ok": true, "killed": killed}))
        }

        _ => {
            json_response(404, &json!({"error": "Not found"}))
        }
    };

    let _ = req.respond(resp);
}
