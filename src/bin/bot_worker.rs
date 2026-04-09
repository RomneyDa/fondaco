//! Bot worker binary — runs a single strategy against live markets.
//!
//! Message-driven process that receives ticks from a collector's Unix socket
//! and executes trades via the CLOB bridge HTTP API.
//!
//! Usage:
//!     cargo run --bin bot-worker -- --slug-prefix btc-updown-5m- --strategy PoissonKelly

use std::collections::HashMap;
use std::fs;
use std::io::Write as IoWrite;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::Utc;
use clap::Parser;
use log::{error, info, warn};
use reqwest::blocking::Client;
use serde_json::Value;

use fondaco::defaults::{
    API_TIMEOUT_DEFAULT, CLOB_MIN_ORDER_TOKENS, DEFAULT_EXPIRATION_SECONDS, TOKEN_DECIMALS,
};
use fondaco::notifications::{NotificationEvent, NotificationManager};
use fondaco::models::MarketMetadata;
use fondaco::session_io::write_pid_file;
use fondaco::strategies::all_strategies;
use fondaco::strategy::Strategy;
use fondaco::tick_broadcast::{TickMessage, TickSubscriber};
use fondaco::trader::{FlatlineDetector, Position, SessionMetadata, TickContext, TradeAction};

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

#[derive(Parser)]
#[command(name = "bot-worker", about = "Run a single trading strategy against live markets")]
struct Cli {
    /// Market slug prefix, e.g. "btc-updown-5m-"
    #[arg(long)]
    slug_prefix: String,

    /// Strategy name (must be in the registry)
    #[arg(long)]
    strategy: String,

    /// Starting cash / bankroll
    #[arg(long, default_value_t = 100.0)]
    cash: f64,

    /// Session cash ratio as R:M (e.g. "0.25:3")
    #[arg(long)]
    session_cash_ratio: Option<String>,

    /// CLOB bridge URL
    #[arg(long, default_value = "http://127.0.0.1:8421")]
    clob_bridge: String,

    /// Data directory (default: "data")
    #[arg(long, default_value = "data")]
    data_dir: String,
}

// ---------------------------------------------------------------------------
// ClobExecutor — executes trades through the CLOB bridge HTTP API
// ---------------------------------------------------------------------------

struct ClobExecutor {
    client: Client,
    bridge_url: String,
    last_sell_fail_time: Option<Instant>,
}

const SELL_COOLDOWN_S: f64 = 5.0;

impl ClobExecutor {
    fn new(bridge_url: &str) -> Self {
        let client = Client::builder()
            .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
            .build()
            .expect("Failed to build CLOB bridge HTTP client");
        Self {
            client,
            bridge_url: bridge_url.trim_end_matches('/').to_string(),
            last_sell_fail_time: None,
        }
    }

    /// Execute a trade action via the CLOB bridge.
    fn execute_trade(
        &mut self,
        action: &TradeAction,
        metadata: &MarketMetadata,
        position: &mut Position,
    ) -> TradeResult {
        if action.is_hold() || action.amount <= 0.0 {
            return TradeResult::skip();
        }

        let is_sell = action.side.starts_with("sell");

        // Sell cooldown after failures
        if is_sell {
            if let Some(fail_time) = self.last_sell_fail_time {
                if fail_time.elapsed().as_secs_f64() < SELL_COOLDOWN_S {
                    return TradeResult::skip();
                }
            }
        }

        let is_up = action.side.contains("up");
        let token_id = if is_up {
            metadata.up_token_id.as_deref().unwrap_or("")
        } else {
            metadata.down_token_id.as_deref().unwrap_or("")
        };

        if token_id.is_empty() {
            return TradeResult::error("missing token_id");
        }

        // Determine order type and price
        let (order_type, price) = if let Some(p) = action.price {
            ("GTD", p)
        } else {
            return TradeResult::error("FAK not supported without book data");
        };

        // Compute trade size
        let (tokens, _cost) = position.compute_trade_size(&action.side, Some(price), action.amount);
        if tokens <= 0.0 || tokens < CLOB_MIN_ORDER_TOKENS {
            return TradeResult::error("insufficient size");
        }

        // Balance reconciliation for sells
        if is_sell {
            match self.get_balance(token_id) {
                Some(balance) => {
                    let needed = tokens;
                    if balance < needed - 0.01 {
                        warn!(
                            "Balance reconciliation: have {:.4} need {:.4} for {} sell",
                            balance, needed, if is_up { "up" } else { "down" }
                        );
                        return TradeResult::error("insufficient balance on-chain");
                    }
                }
                None => {
                    warn!("Could not fetch balance for sell reconciliation");
                }
            }
        }

        let clob_side = if action.side.starts_with("buy") {
            "BUY"
        } else {
            "SELL"
        };

        let expiration = action
            .expiration_seconds
            .unwrap_or(DEFAULT_EXPIRATION_SECONDS);

        let body = serde_json::json!({
            "token_id": token_id,
            "side": clob_side,
            "price": format!("{:.2}", price),
            "size": format!("{:.4}", tokens),
            "order_type": order_type,
            "expiration_seconds": expiration as u64,
        });

        let url = format!("{}/create_and_post_order", self.bridge_url);
        let resp = match self.client.post(&url).json(&body).send() {
            Ok(r) => r,
            Err(e) => {
                if is_sell {
                    self.last_sell_fail_time = Some(Instant::now());
                }
                return TradeResult::error(&format!("bridge request failed: {}", e));
            }
        };

        if !resp.status().is_success() {
            let status = resp.status();
            let body_text = resp.text().unwrap_or_default();
            if is_sell {
                self.last_sell_fail_time = Some(Instant::now());
            }
            return TradeResult::error(&format!("bridge returned {}: {}", status, body_text));
        }

        let resp_json: Value = match resp.json() {
            Ok(j) => j,
            Err(e) => {
                return TradeResult::error(&format!("bridge response parse error: {}", e));
            }
        };

        let order_id = resp_json
            .get("order_id")
            .or_else(|| resp_json.get("orderID"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let success = resp_json
            .get("success")
            .and_then(|v| v.as_bool())
            .unwrap_or(true);

        if !success {
            let err_msg = resp_json
                .get("error")
                .or_else(|| resp_json.get("errorMsg"))
                .and_then(|v| v.as_str())
                .unwrap_or("unknown error");
            if is_sell {
                self.last_sell_fail_time = Some(Instant::now());
            }
            return TradeResult {
                executed: true,
                success: false,
                order_id,
                tokens: 0.0,
                price: 0.0,
                fee: 0.0,
                error: Some(err_msg.to_string()),
            };
        }

        position.apply_trade(&action.side, price, tokens);
        let fee = fondaco::defaults::compute_taker_fee(price, tokens, true);
        position.cash -= fee;

        if is_sell {
            self.last_sell_fail_time = None;
        }

        TradeResult {
            executed: true,
            success: true,
            order_id,
            tokens,
            price,
            fee,
            error: None,
        }
    }

    fn get_balance(&self, token_id: &str) -> Option<f64> {
        let url = format!("{}/balance", self.bridge_url);
        let resp = self
            .client
            .get(&url)
            .query(&[("token_id", token_id)])
            .timeout(Duration::from_secs(5))
            .send()
            .ok()?;
        if !resp.status().is_success() {
            return None;
        }
        let json: Value = resp.json().ok()?;
        json.get("balance")
            .and_then(|v| {
                v.as_f64()
                    .or_else(|| v.as_str().and_then(|s| s.parse::<f64>().ok()))
            })
    }

    #[allow(dead_code)]
    fn cancel_order(&self, order_id: &str) -> bool {
        let url = format!("{}/cancel_order", self.bridge_url);
        let body = serde_json::json!({ "order_id": order_id });
        match self.client.post(&url).json(&body).send() {
            Ok(resp) => resp.status().is_success(),
            Err(e) => {
                warn!("Cancel order {} failed: {}", order_id, e);
                false
            }
        }
    }

    #[allow(dead_code)]
    fn get_order_status(&self, order_id: &str) -> Option<Value> {
        let url = format!("{}/get_order", self.bridge_url);
        let resp = self
            .client
            .get(&url)
            .query(&[("order_id", order_id)])
            .timeout(Duration::from_secs(5))
            .send()
            .ok()?;
        if !resp.status().is_success() {
            return None;
        }
        resp.json().ok()
    }
}

struct TradeResult {
    executed: bool,
    success: bool,
    order_id: Option<String>,
    tokens: f64,
    price: f64,
    fee: f64,
    error: Option<String>,
}

impl TradeResult {
    fn skip() -> Self {
        Self {
            executed: false,
            success: false,
            order_id: None,
            tokens: 0.0,
            price: 0.0,
            fee: 0.0,
            error: None,
        }
    }

    fn error(msg: &str) -> Self {
        Self {
            executed: true,
            success: false,
            order_id: None,
            tokens: 0.0,
            price: 0.0,
            fee: 0.0,
            error: Some(msg.to_string()),
        }
    }
}

// ---------------------------------------------------------------------------
// Trade logging
// ---------------------------------------------------------------------------

fn round_token(v: f64) -> f64 {
    let factor = 10f64.powi(TOKEN_DECIMALS as i32);
    (v * factor).round() / factor
}

fn log_trade(
    market_dir: &str,
    strategy_name: &str,
    tick_index: usize,
    elapsed_seconds: f64,
    side: &str,
    price: f64,
    tokens: f64,
    success: bool,
    order_id: Option<&str>,
    fee: f64,
    error: Option<&str>,
) {
    let trades_file = PathBuf::from(market_dir).join("bot_trades.jsonl");
    let entry = serde_json::json!({
        "timestamp": Utc::now().to_rfc3339(),
        "strategy": strategy_name,
        "tick_index": tick_index,
        "elapsed_seconds": elapsed_seconds,
        "side": side,
        "price": price,
        "tokens": round_token(tokens),
        "amount": (tokens * price * 100.0).round() / 100.0,
        "success": success,
        "order_id": order_id,
        "fee": (fee * 1_000_000.0).round() / 1_000_000.0,
        "error": error,
    });
    if let Ok(mut f) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&trades_file)
    {
        let _ = writeln!(f, "{}", serde_json::to_string(&entry).unwrap_or_default());
    }
}

fn write_position_snapshot(
    market_dir: &str,
    strategy_name: &str,
    position: &Position,
    bankroll: f64,
    session_count: u64,
    session_cash_ratio: Option<(f64, f64)>,
) {
    let positions_dir = PathBuf::from(market_dir).join("bot_positions");
    let _ = fs::create_dir_all(&positions_dir);
    let filepath = positions_dir.join(format!("{}.json", strategy_name));
    let mut data = serde_json::json!({
        "strategy": strategy_name,
        "cash": round_token(position.cash),
        "up_tokens": round_token(position.up_tokens),
        "down_tokens": round_token(position.down_tokens),
        "bankroll": round_token(bankroll),
        "session_count": session_count,
        "timestamp": Utc::now().to_rfc3339(),
        "pid": std::process::id(),
    });
    if let Some((ratio, max)) = session_cash_ratio {
        data["session_cash_ratio"] = serde_json::json!({ "ratio": ratio, "max": max });
    }
    let tmp = filepath.with_extension("json.tmp");
    if let Ok(mut f) = fs::File::create(&tmp) {
        let _ = serde_json::to_writer_pretty(&mut f, &data);
        let _ = fs::rename(&tmp, &filepath);
    }
}

fn log_session_result(
    market_dir: &str,
    strategy_name: &str,
    outcome: Option<&str>,
    trade_count: usize,
    bankroll: f64,
    pnl: f64,
    credit: f64,
    session_count: u64,
) {
    let log_file = PathBuf::from(market_dir).join("bot_log.jsonl");
    let entry = serde_json::json!({
        "timestamp": Utc::now().to_rfc3339(),
        "strategy": strategy_name,
        "outcome": outcome,
        "trades": trade_count,
        "cash": round_token(bankroll),
        "pnl": round_token(pnl),
        "credit": round_token(credit),
        "session_number": session_count,
    });
    if let Ok(mut f) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_file)
    {
        let _ = writeln!(f, "{}", serde_json::to_string(&entry).unwrap_or_default());
    }
}

// ---------------------------------------------------------------------------
// Session cash ratio parsing
// ---------------------------------------------------------------------------

fn parse_session_cash_ratio(s: &str) -> Option<(f64, f64)> {
    let parts: Vec<&str> = s.split(':').collect();
    if parts.len() != 2 {
        return None;
    }
    let ratio: f64 = parts[0].parse().ok()?;
    let max_val: f64 = parts[1].parse().ok()?;
    Some((ratio, max_val))
}

// ---------------------------------------------------------------------------
// Main bot worker — message-driven loop
// ---------------------------------------------------------------------------

fn run_bot_worker(cli: Cli) {
    let slug_key = cli.slug_prefix.trim_end_matches('-').to_string();

    let session_cash_ratio = cli
        .session_cash_ratio
        .as_deref()
        .and_then(parse_session_cash_ratio);

    // Look up strategy in registry
    let registry = all_strategies();
    let strategy_factory = match registry.get(&cli.strategy) {
        Some(f) => *f,
        None => {
            error!(
                "Unknown strategy '{}'. Available: {:?}",
                cli.strategy,
                registry.keys().collect::<Vec<_>>()
            );
            std::process::exit(1);
        }
    };

    let notifier = NotificationManager::load();

    info!(
        "[{}] Bot worker starting. Strategy={}, Cash=${:.2}, Bridge={}",
        slug_key, cli.strategy, cli.cash, cli.clob_bridge
    );

    notifier.notify(
        NotificationEvent::ProcessStart,
        "Bot Worker Started",
        format!("{} on {} — ${:.2}", cli.strategy, slug_key, cli.cash),
    );

    let running = Arc::new(AtomicBool::new(true));
    {
        let r = Arc::clone(&running);
        ctrlc::set_handler(move || {
            info!("Shutdown signal received.");
            r.store(false, Ordering::SeqCst);
        })
        .expect("Error setting Ctrl-C handler");
    }

    // PID file
    let pid_path = format!("/tmp/fondaco-{}-bot-{}.pid", slug_key, cli.strategy);
    write_pid_file(&pid_path);

    // Market data directory
    let market_dir = format!("{}/markets/{}", cli.data_dir, slug_key);
    let _ = fs::create_dir_all(&market_dir);

    // CLOB executor
    let mut executor = ClobExecutor::new(&cli.clob_bridge);

    let mut bankroll = cli.cash;
    let mut session_count: u64 = 0;

    // Tick socket path
    let sock_path = format!("{}/markets/{}/tick.sock", cli.data_dir, slug_key);

    while running.load(Ordering::SeqCst) {
        info!("[{}:{}] Connecting to tick socket: {}", slug_key, cli.strategy, sock_path);
        let mut subscriber = match TickSubscriber::connect(&sock_path, &running) {
            Some(s) => s,
            None => break, // shutdown
        };

        // Per-session state
        let mut strategy: Option<Box<dyn Strategy>> = None;
        let mut position: Option<Position> = None;
        let mut session_meta: Option<SessionMetadata> = None;
        let mut metadata: Option<MarketMetadata> = None;
        let mut initial_btc_price = 0.0_f64;
        let mut trade_count: usize = 0;
        let mut session_reserve = 0.0_f64;
        let mut starting_cash = bankroll;
        let mut window_duration: u64 = 0;
        let mut skip_session = false;
        let mut flatline = FlatlineDetector::new();

        while running.load(Ordering::SeqCst) {
            let msg = match subscriber.next_message() {
                Some(m) => m,
                None => {
                    warn!("[{}:{}] Tick socket disconnected, reconnecting...", slug_key, cli.strategy);
                    break; // reconnect outer loop
                }
            };

            match msg {
                TickMessage::SessionStart {
                    window_ts: _,
                    window_end_ts: _,
                    window_duration: wd,
                    initial_btc_price: _,
                    prior_session_summaries,
                    gap_before_seconds,
                    data_density,
                    event_slug: _,
                    event_title,
                    up_token_id,
                    down_token_id,
                    window_start_utc,
                    window_end_utc: _,
                    condition_id: _,
                    replayed,
                } => {
                    window_duration = wd;

                    // Check if strategy requires full session and we joined mid-session
                    let mut strat = strategy_factory();
                    if replayed && strat.requires_full_session() {
                        info!(
                            "[{}:{}] Joined mid-session, strategy requires full session — skipping",
                            slug_key, cli.strategy
                        );
                        skip_session = true;
                        continue;
                    }
                    skip_session = false;

                    // Compute session cash
                    let mut deploy_cash = bankroll;
                    session_reserve = 0.0;
                    if let Some((ratio, cap)) = session_cash_ratio {
                        let deploy = (deploy_cash * ratio).min(cap);
                        if deploy_cash > deploy {
                            session_reserve = deploy_cash - deploy;
                            deploy_cash = deploy;
                        }
                    }
                    starting_cash = bankroll;

                    let pos = Position::new(deploy_cash);

                    let meta = SessionMetadata {
                        up_token_id: up_token_id.clone().unwrap_or_default(),
                        down_token_id: down_token_id.clone().unwrap_or_default(),
                        window_start_utc: window_start_utc.clone().unwrap_or_default(),
                        window_duration: wd as f64,
                        prior_session_summaries,
                        gap_before_seconds,
                        data_density,
                        extra: HashMap::new(),
                    };

                    strat.on_session_start(&meta);

                    let md = MarketMetadata {
                        event_id: None,
                        event_slug: None,
                        event_title: event_title.clone(),
                        condition_id: None,
                        up_token_id,
                        down_token_id,
                        window_start_utc,
                        window_end_utc: None,
                        resolution_source: None,
                    };

                    info!(
                        "[{}:{}] Session starting: {} | Deploying ${:.2} (reserve ${:.2})",
                        slug_key,
                        cli.strategy,
                        event_title.as_deref().unwrap_or("(unknown)"),
                        deploy_cash,
                        session_reserve,
                    );

                    write_position_snapshot(&market_dir, &cli.strategy, &pos, bankroll, session_count, session_cash_ratio);

                    initial_btc_price = 0.0;
                    trade_count = 0;
                    strategy = Some(strat);
                    position = Some(pos);
                    session_meta = Some(meta);
                    metadata = Some(md);
                }

                TickMessage::Tick { sample, tick_index } => {
                    if skip_session {
                        continue;
                    }
                    let strat = match strategy.as_mut() {
                        Some(s) => s,
                        None => continue, // no session started yet
                    };
                    let pos = position.as_mut().unwrap();
                    let meta = session_meta.as_ref().unwrap();
                    let md = metadata.as_ref().unwrap();

                    if tick_index == 0 {
                        initial_btc_price = sample.btc_price_usd.unwrap_or(0.0);
                    }

                    let elapsed_frac = if window_duration > 0 {
                        (sample.elapsed_seconds / window_duration as f64).min(1.0)
                    } else {
                        0.0
                    };

                    let ctx = TickContext {
                        tick_index,
                        sample: &sample,
                        position: pos,
                        metadata: meta,
                        initial_btc_price,
                        elapsed_fraction: elapsed_frac,
                        elapsed_seconds: sample.elapsed_seconds,
                        max_buy_amount: pos.cash,
                        open_orders: Vec::new(),
                    };

                    let was_flatlined = flatline.is_flatlined;
                    let trading_allowed = flatline.update(&sample);
                    if !was_flatlined && flatline.is_flatlined {
                        notifier.notify(
                            NotificationEvent::ClobDown,
                            "CLOB Down — Flatline Detected",
                            format!(
                                "Midpoints stale on {} — trading disabled for {}",
                                slug_key, cli.strategy,
                            ),
                        );
                    }

                    let action = strat.on_tick(&ctx);
                    let logs = strat.drain_logs();
                    for msg in &logs {
                        log::debug!("[{}] {}", strat.name(), msg);
                    }

                    // Execute trade if not hold
                    if !action.is_hold() && action.amount > 0.0 && trading_allowed {
                        // Guardrail: block trades near session end
                        if sample.elapsed_seconds < (window_duration as f64 - 5.0) {
                            let result = executor.execute_trade(&action, md, pos);
                            if result.executed {
                                log_trade(
                                    &market_dir,
                                    &cli.strategy,
                                    tick_index,
                                    sample.elapsed_seconds,
                                    &action.side,
                                    result.price,
                                    result.tokens,
                                    result.success,
                                    result.order_id.as_deref(),
                                    result.fee,
                                    result.error.as_deref(),
                                );

                                if result.success {
                                    trade_count += 1;
                                    info!(
                                        "[{}:{}] Trade: {} @ {:.2} x {:.2} | order={}",
                                        slug_key,
                                        cli.strategy,
                                        action.side,
                                        result.price,
                                        result.tokens,
                                        result.order_id.as_deref().unwrap_or("-"),
                                    );
                                    let event = if action.side.contains("buy") {
                                        NotificationEvent::TradeBuy
                                    } else {
                                        NotificationEvent::TradeSell
                                    };
                                    notifier.notify(
                                        event,
                                        &format!("{} — {}", cli.strategy, action.side),
                                        format!(
                                            "{:.2} tokens @ {:.2} on {}",
                                            result.tokens, result.price, slug_key,
                                        ),
                                    );
                                } else {
                                    warn!(
                                        "[{}:{}] Trade failed: {} | {}",
                                        slug_key,
                                        cli.strategy,
                                        action.side,
                                        result.error.as_deref().unwrap_or("unknown"),
                                    );
                                    strat.on_trade_failed(&action.side);
                                    notifier.notify(
                                        NotificationEvent::Error,
                                        &format!("{} — trade failed", cli.strategy),
                                        format!(
                                            "{} on {} — {}",
                                            action.side,
                                            slug_key,
                                            result.error.as_deref().unwrap_or("unknown"),
                                        ),
                                    );
                                }
                            }
                        }
                    }
                }

                TickMessage::SessionEnd { outcome, .. } => {
                    if skip_session {
                        skip_session = false;
                        continue;
                    }
                    let pos = match position.as_mut() {
                        Some(p) => p,
                        None => continue,
                    };

                    // Compute credit before resolving
                    let outcome_ref = outcome.as_deref();
                    let credit = match outcome_ref {
                        Some("Up") => pos.up_tokens,
                        Some("Down") => pos.down_tokens,
                        _ => 0.0,
                    };

                    pos.resolve(outcome_ref);
                    pos.cash += session_reserve;

                    bankroll = pos.cash;
                    let pnl = bankroll - starting_cash;

                    info!(
                        "[{}:{}] Resolved: {:?} | Trades: {} | Bankroll: ${:.2} | PnL: ${:.4}",
                        slug_key, cli.strategy, outcome, trade_count, bankroll, pnl
                    );

                    if trade_count > 0 {
                        let event = if pnl >= 0.0 {
                            NotificationEvent::SessionEndWin
                        } else {
                            NotificationEvent::SessionEndLoss
                        };
                        let sign = if pnl >= 0.0 { "+" } else { "" };
                        notifier.notify(
                            event,
                            &format!(
                                "{} — {} {}",
                                cli.strategy,
                                outcome.as_deref().unwrap_or("?"),
                                if pnl >= 0.0 { "✓" } else { "✗" },
                            ),
                            format!(
                                "{}${:.4} | {} trades | Bankroll ${:.2}",
                                sign, pnl, trade_count, bankroll,
                            ),
                        );
                    }

                    log_session_result(
                        &market_dir,
                        &cli.strategy,
                        outcome_ref,
                        trade_count,
                        bankroll,
                        pnl,
                        credit,
                        session_count,
                    );
                    session_count += 1;
                    write_position_snapshot(&market_dir, &cli.strategy, pos, bankroll, session_count, session_cash_ratio);

                    // Clear session state
                    strategy = None;
                    position = None;
                    session_meta = None;
                    metadata = None;
                }

                TickMessage::Waiting { .. } => {
                    // Idle — nothing to do
                }
            }
        }
    }

    fondaco::session_io::remove_file_silent(&pid_path);
    info!(
        "[{}:{}] Bot worker stopped. {} sessions completed.",
        slug_key, cli.strategy, session_count
    );
    notifier.notify(
        NotificationEvent::ProcessStop,
        "Bot Worker Stopped",
        format!("{} on {} — {} sessions", cli.strategy, slug_key, session_count),
    );
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .format(|buf, record| {
            let ts = chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f");
            writeln!(buf, "{} [{}] {}", ts, record.level(), record.args())
        })
        .init();

    let cli = Cli::parse();
    run_bot_worker(cli);
}
