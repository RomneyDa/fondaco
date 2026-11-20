"use client";

import { useState } from "react";

// ── Diagram primitives (HTML/CSS) ───────────────────────────────

const C = {
  rust: { bg: "rgba(210,153,34,0.08)", border: "rgba(210,153,34,0.3)", text: "#d29922" },
  python: { bg: "rgba(63,185,80,0.08)", border: "rgba(63,185,80,0.3)", text: "#3fb950" },
  ts: { bg: "rgba(88,166,255,0.08)", border: "rgba(88,166,255,0.3)", text: "#58a6ff" },
  ipc: { bg: "rgba(57,210,192,0.06)", border: "rgba(57,210,192,0.25)", text: "#39d2c0" },
  ext: { bg: "rgba(188,140,255,0.06)", border: "rgba(188,140,255,0.25)", text: "#bc8cff" },
};

function Node({ label, sub, detail, scheme, dashed, className = "" }: {
  label: string; sub?: string; detail?: string[];
  scheme: typeof C.rust; dashed?: boolean; className?: string;
}) {
  return (
    <div className={`rounded-lg px-4 py-3 ${className}`}
      style={{ background: scheme.bg, border: `1.5px ${dashed ? "dashed" : "solid"} ${scheme.border}` }}>
      <div className="text-[13px] font-semibold text-white">{label}</div>
      {sub && <div className="text-[10px] mt-0.5" style={{ color: scheme.text }}>{sub}</div>}
      {detail && detail.map((d, i) => (
        <div key={i} className="text-[10px] text-dim mt-0.5">{d}</div>
      ))}
    </div>
  );
}

function Connector({ label, color = "#30363d", dashed }: { label?: string; color?: string; dashed?: boolean }) {
  return (
    <div className="flex flex-col items-center gap-0 py-1">
      <div className="w-px h-4" style={{ background: color, borderLeft: dashed ? `1px dashed ${color}` : undefined, width: dashed ? 0 : 1 }} />
      {label && <span className="text-[9px] italic" style={{ color: "#8b949e" }}>{label}</span>}
      <div className="w-px h-2" style={{ background: color, width: dashed ? 0 : 1 }} />
      <div style={{ width: 0, height: 0, borderLeft: "4px solid transparent", borderRight: "4px solid transparent", borderTop: `5px solid ${color}` }} />
    </div>
  );
}

function HArrow({ label, color = "#8b949e", dashed, reverse }: { label?: string; color?: string; dashed?: boolean; reverse?: boolean }) {
  return (
    <div className="flex items-center gap-1 px-1 shrink-0">
      {reverse && <div style={{ width: 0, height: 0, borderTop: "4px solid transparent", borderBottom: "4px solid transparent", borderRight: `5px solid ${color}` }} />}
      <div className="flex-1 min-w-[20px] h-px" style={{ background: color, borderTop: dashed ? `1px dashed ${color}` : undefined, height: dashed ? 0 : 1 }} />
      {label && <span className="text-[9px] italic whitespace-nowrap" style={{ color }}>{label}</span>}
      <div className="flex-1 min-w-[20px] h-px" style={{ background: color, borderTop: dashed ? `1px dashed ${color}` : undefined, height: dashed ? 0 : 1 }} />
      {!reverse && <div style={{ width: 0, height: 0, borderTop: "4px solid transparent", borderBottom: "4px solid transparent", borderLeft: `5px solid ${color}` }} />}
    </div>
  );
}

function Badge({ text, color }: { text: string; color: string }) {
  return (
    <span className="inline-block text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded-full border"
      style={{ color, borderColor: `${color}50`, background: `${color}15` }}>
      {text}
    </span>
  );
}

// ── System diagram ──────────────────────────────────────────────

function SystemDiagram() {
  return (
    <div className="space-y-3 text-sm">
      {/* External services */}
      <div className="rounded-lg px-4 py-2.5 flex flex-wrap gap-6 justify-center"
        style={{ background: C.ext.bg, border: `1px solid ${C.ext.border}` }}>
        <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: C.ext.text }}>External</span>
        {["Polymarket CLOB", "Gamma API", "RTDS WebSocket", "Coinbase / CoinGecko"].map(s => (
          <span key={s} className="text-[11px] text-dim">{s}</span>
        ))}
      </div>

      {/* Row: arrows down */}
      <div className="flex gap-3 items-start px-2">
        <div className="text-[9px] text-dim text-center flex-1">CLOB polling + metadata + RTDS price</div>
        <div className="text-[9px] text-dim text-center flex-1 opacity-0">spacer</div>
      </div>

      {/* Row 1: Orchestrator | clob-bridge | Dashboard */}
      <div className="grid grid-cols-[1fr_1fr_1fr] gap-3">
        <Node label="Orchestrator" sub=":8420 · optional" scheme={C.rust} dashed
          detail={["Spawns collectors + workers", "HTTP control API"]} />
        <Node label="clob-bridge" sub="Python Flask · :8421" scheme={C.python}
          detail={["py_clob_client SDK wrapper", "Signs + posts limit orders"]} />
        <Node label="Dashboard" sub="Next.js + tRPC · :3456 · loopback" scheme={C.ts}
          detail={["SSE file watching", "Proxies to orchestrator"]} />
      </div>

      {/* Arrows: orchestrator spawns down, workers → bridge, dashboard → orchestrator */}
      <div className="grid grid-cols-[1fr_1fr_1fr] gap-3 items-center">
        <div className="flex justify-center">
          <Connector label="spawns" color="#d2992260" dashed />
        </div>
        <div className="flex justify-center">
          <span className="text-[9px] text-dim">signed orders to CLOB</span>
        </div>
        <div className="flex justify-center">
          <Connector label="SSE + file watch" color="#58a6ff40" dashed />
        </div>
      </div>

      {/* Row 2: Collector */}
      <div className="grid grid-cols-[2fr_1fr] gap-3">
        <Node label="Collector" sub="1 per market · Rust" scheme={C.rust}
          detail={[
            "RTDS WebSocket + CLOB polling",
            "SessionStatsCache (prior summaries, gap, density)",
            "Writes sessions/*.json + .summary.json",
            "Broadcasts ticks on tick.sock",
          ]} />
        <Node label="Backtest CLI" sub="Rust · rayon parallel" scheme={C.rust}
          detail={[
            "Reads sessions/ + .summary.json",
            "Param sweeps, Kelly sizing",
            "Generates missing sidecars",
          ]} />
      </div>

      {/* tick.sock */}
      <Connector label="" color={C.ipc.text} />
      <div className="rounded-md px-4 py-2 text-center"
        style={{ background: C.ipc.bg, border: `1px dashed ${C.ipc.border}` }}>
        <span className="text-[11px] font-mono" style={{ color: C.ipc.text }}>
          tick.sock — SessionStart → Tick → SessionEnd → Waiting
        </span>
      </div>
      <Connector color={C.ipc.text} />

      {/* Row 3: Bot Workers */}
      <div className="grid grid-cols-[1fr_1fr_1fr] gap-3">
        <Node label="Bot Worker" sub="PoissonKelly · Rust" scheme={C.rust}
          detail={["TickSubscriber (reads tick.sock)", "strategy.on_tick() → TradeAction", "ClobExecutor → clob-bridge"]} />
        <Node label="Bot Worker" sub="DriftSniper · Rust" scheme={C.rust}
          detail={["Same architecture", "Different strategy instance"]} />
        <Node label="Claim Server" sub="Python · :8080" scheme={C.python}
          detail={["Periodic position redemption", "Builder Relayer API"]} />
      </div>

      {/* Arrows down to data */}
      <Connector label="writes trades, reads sessions" color="#39d2c080" />

      {/* Data layer */}
      <Node label="data/markets/{slug}/" scheme={C.ipc}
        detail={[
          "sessions/*.json + .summary.json (collected data)",
          "bot_trades.jsonl · bot_log.jsonl (trading logs)",
          "bot_positions/{strategy}.json · live/session.json",
          "tick.sock (Unix domain socket)",
        ]} />

      {/* Legend */}
      <div className="flex flex-wrap gap-4 pt-2 text-[10px]">
        <span className="font-semibold text-dim uppercase tracking-wider">Legend:</span>
        {[
          { label: "Rust", ...C.rust },
          { label: "Python", ...C.python },
          { label: "TypeScript", ...C.ts },
          { label: "Data / IPC", ...C.ipc },
          { label: "External", ...C.ext },
        ].map(({ label, bg, border, text }) => (
          <span key={label} className="inline-flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-sm inline-block" style={{ background: bg, border: `1px solid ${border}` }} />
            <span style={{ color: text }}>{label}</span>
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5">
          <span className="w-5 h-0 inline-block border-t border-dashed" style={{ borderColor: "#8b949e" }} />
          <span className="text-dim">optional</span>
        </span>
      </div>
    </div>
  );
}

// ── Data flow diagram ───────────────────────────────────────────

function DataFlowDiagram() {
  const steps = [
    { label: "Collector connects to RTDS WebSocket + polls CLOB API", color: "#d29922" },
    { label: "At window boundary: loads SessionStatsCache, fetches metadata from Gamma", color: "#d29922" },
    { label: "Broadcasts SessionStart (prior summaries, gap, density) on tick.sock", color: "#39d2c0" },
    { label: "Every ~55ms: takes sample, broadcasts Tick { sample, tick_index }", color: "#39d2c0" },
    { label: "Bot worker receives tick, builds TickContext, calls strategy.on_tick()", color: "#d29922" },
    { label: "If trade: ClobExecutor → clob-bridge HTTP → signed order to CLOB", color: "#3fb950" },
    { label: "Window ends: collector broadcasts SessionEnd { outcome }", color: "#39d2c0" },
    { label: "Bot worker resolves position, updates bankroll, logs to bot_trades.jsonl", color: "#d29922" },
    { label: "Collector writes session.json + .summary.json, refreshes stats cache", color: "#d29922" },
  ];

  return (
    <div className="flex flex-col gap-0">
      {steps.map((step, i) => (
        <div key={i} className="flex items-start gap-3 relative">
          <div className="flex flex-col items-center shrink-0" style={{ width: 28 }}>
            <div className="w-6 h-6 rounded-full border-2 flex items-center justify-center text-[10px] font-bold shrink-0"
              style={{ borderColor: step.color, color: step.color }}>
              {i + 1}
            </div>
            {i < steps.length - 1 && (
              <div className="w-px flex-1 min-h-[16px]" style={{ background: "#30363d" }} />
            )}
          </div>
          <p className="text-sm text-[#c9d1d9] pb-3 pt-0.5">{step.label}</p>
        </div>
      ))}
    </div>
  );
}

// ── Section components ──────────────────────────────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-10">
      <h2 className="text-lg font-semibold text-accent border-b border-border pb-2 mb-4">{title}</h2>
      {children}
    </section>
  );
}

function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={`bg-card border border-border rounded-lg p-4 ${className}`}>
      {children}
    </div>
  );
}

// ── Main page ───────────────────────────────────────────────────

export function ArchitecturePage() {
  const [expandedSection, setExpandedSection] = useState<string | null>(null);
  const toggle = (s: string) => setExpandedSection(expandedSection === s ? null : s);

  return (
    <div className="p-6 max-w-[1100px] mx-auto overflow-y-auto h-full">
      <h1 className="text-2xl font-bold text-white mb-1">Architecture</h1>
      <p className="text-dim text-sm mb-8">Polymarket up/down trading system — Rust core, Python bridge, Next.js dashboard.</p>

      {/* ── System diagram ── */}
      <Section title="System Overview">
        <Card>
          <SystemDiagram />
        </Card>
        <p className="text-xs text-dim mt-2">
          The orchestrator is <strong>optional</strong> — collectors and bot-workers can be started independently via CLI.
          The orchestrator adds process lifecycle management and a control API that the dashboard proxies to.
        </p>
      </Section>

      {/* ── Binaries ── */}
      <Section title="Binaries">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {[
            { name: "orchestrator", lang: "Rust", color: "#d29922", port: ":8420",
              desc: "Process manager. Starts/stops clob-bridge, collectors, and bot workers. Optional — all processes can run standalone.",
              cmd: "./target/release/orchestrator [--port 8420] [--no-clob-bridge]" },
            { name: "collector", lang: "Rust", color: "#d29922", port: "—",
              desc: "One per market. RTDS WebSocket + CLOB polling. Writes session JSON + .summary.json sidecars. Broadcasts ticks via Unix socket.",
              cmd: "./target/release/collector --slug-prefix btc-updown-5m-" },
            { name: "bot-worker", lang: "Rust", color: "#d29922", port: "—",
              desc: "One per strategy per market. Connects to collector's tick.sock. Runs strategy, executes trades via clob-bridge.",
              cmd: "./target/release/bot-worker --slug-prefix btc-updown-5m- --strategy PoissonKelly" },
            { name: "backtest", lang: "Rust", color: "#d29922", port: "—",
              desc: "Offline strategy backtester. Rayon-parallel param sweeps, Kelly sizing, Sharpe ranking. Generates missing .summary.json sidecars.",
              cmd: "./target/release/backtest data/markets/btc-updown-5m/sessions --strategy all --recent 30" },
            { name: "sports-collector", lang: "Rust", color: "#d29922", port: "—",
              desc: "One per league, many concurrent games. Polymarket sports WebSocket for live state + one multi-token CLOB socket for books. Adaptive per-game cadence (dormant \u2192 pregame \u2192 idle \u2192 hot). Appends per-game JSONL and measures feed latency. Read-only.",
              cmd: "./target/release/sports-collector --league nfl" },
            { name: "sports-bot-worker", lang: "Rust", color: "#d29922", port: "—",
              desc: "One per strategy per league, holding a map of concurrent games. Owns the portfolio allocator and the event-confirmation gate. Dry-run unless --live.",
              cmd: "./target/release/sports-bot-worker --league nfl --strategy StaleBook" },
            { name: "sports-backtest", lang: "Rust", color: "#d29922", port: "—",
              desc: "Overlapping-game event-time simulator with depth-aware fills. Headline metrics are calibration and closing line value, not PnL \u2014 272 NFL games a year gives PnL almost no statistical power.",
              cmd: "./target/release/sports-backtest --league nfl --walk-forward" },
            { name: "clob-bridge", lang: "Python", color: "#3fb950", port: ":8421",
              desc: "Flask wrapper around py_clob_client SDK. Signs and posts limit orders to Polymarket CLOB. Manages Gnosis Safe auth.",
              cmd: "cd clob-bridge && uv run python main.py --port 8421" },
            { name: "model-server", lang: "Python", color: "#3fb950", port: ":8422",
              desc: "Fits Dixon-Coles team strengths offline and serves scoring *rates*, never per-tick probabilities. Python fits, Rust evaluates \u2014 the fit only changes when results arrive, weekly, not when the clock moves.",
              cmd: "cd model-server && uv run python main.py --port 8422" },
            { name: "claim-server", lang: "Python", color: "#3fb950", port: ":8080",
              desc: "Periodic position redemption via Builder Relayer API. Web UI for manual claims.",
              cmd: "cd claim-server && uv run python main.py" },
            { name: "dashboard", lang: "TypeScript", color: "#58a6ff", port: ":3456",
              desc: "Next.js 15 (App Router) + tRPC + Express. SSE file-watching for live updates. Proxies bot control to orchestrator.",
              cmd: "cd dashboard && npm run dev" },
          ].map((b) => (
            <Card key={b.name}>
              <div className="flex items-center gap-2 mb-2">
                <Badge text={b.lang} color={b.color} />
                <span className="font-semibold text-white text-sm">{b.name}</span>
                {b.port !== "—" && <span className="text-xs font-mono" style={{ color: b.color }}>{b.port}</span>}
              </div>
              <p className="text-xs text-dim mb-2">{b.desc}</p>
              <code className="block text-[10px] text-[#39d2c0] bg-bg rounded px-2 py-1 break-all">{b.cmd}</code>
            </Card>
          ))}
        </div>
      </Section>

      {/* ── Sports ── */}
      <Section title="Sports">
        <Card>
          <p className="text-xs text-dim mb-3">
            Sports fits the framework by treating <strong>a game as a session</strong>.
            Three things are genuinely different and everything in{" "}
            <code className="text-[#39d2c0]">src/sports/</code> exists to handle them.
          </p>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {[
              {
                title: "Concurrency",
                body: "Up to ~15 games run at once on no clock grid, versus one crypto session at a time. One collector per league holds a single sports feed socket and a single multi-token book socket for the whole slate. A portfolio allocator caps per-game, per-league and total exposure, because thirteen Sunday NFL games resolving together is a fat tail, not thirteen independent bets.",
              },
              {
                title: "Schedule-driven discovery",
                body: "There is no deterministic prefix + floor(now/300)*300 slug. Games come from the Gamma schedule and must be joined to the live results feed by team name. That join is the flakiest part of the system: an ambiguous match is refused rather than guessed, since trading one game on another's scoreline is far worse than not trading.",
              },
              {
                title: "Adaptive cadence",
                body: "A 3.5 hour game at the crypto rate of 18 Hz would be a quarter of a million samples of almost nothing. Sampling follows the game: dormant before kickoff, 0.5 Hz batched while idle, 5\u201310 Hz for ~45s after a score, card or period change, and always in the closing minutes. ~100x fewer requests with full resolution exactly where edge appears.",
              },
            ].map((c) => (
              <div key={c.title} className="rounded-lg px-3 py-2"
                style={{ background: C.ipc.bg, border: `1px solid ${C.ipc.border}` }}>
                <div className="text-[13px] font-semibold text-white mb-1">{c.title}</div>
                <p className="text-[11px] text-dim leading-relaxed">{c.body}</p>
              </div>
            ))}
          </div>

          <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="rounded-lg px-3 py-2"
              style={{ background: C.ext.bg, border: `1px solid ${C.ext.border}` }}>
              <div className="text-[13px] font-semibold text-white mb-1">Fair value, in three layers</div>
              <ul className="text-[11px] text-dim leading-relaxed space-y-1 list-disc pl-4">
                <li><strong>Layer 0</strong> — book staleness after a score, and cross-leg incoherence. No model at all, and possibly the whole business.</li>
                <li><strong>Layer 1</strong> — ESPN&apos;s free live NFL win probability.</li>
                <li><strong>Layer 2</strong> — our own model. Closed-form bivariate Poisson for soccer with Dixon-Coles rho and red-card adjustment; normal-margin model for football.</li>
              </ul>
            </div>
            <div className="rounded-lg px-3 py-2"
              style={{ background: C.ext.bg, border: `1px solid ${C.ext.border}` }}>
              <div className="text-[13px] font-semibold text-white mb-1">Two things that would silently cost money</div>
              <ul className="text-[11px] text-dim leading-relaxed space-y-1 list-disc pl-4">
                <li><strong>Fills.</strong> Sports books have real depth, so the crypto &quot;filled when the midpoint crosses my price&quot; model does not transfer. Backtest fills walk recorded book levels — which is why depth is captured, since it cannot be backfilled.</li>
                <li><strong>Retracted events.</strong> A goal disallowed by VAR, a touchdown reversed on review. Guarded by a confirmation delay, two-feed agreement with ESPN, and outright suspension while a review flag is set.</li>
              </ul>
            </div>
          </div>

          <p className="text-[11px] text-dim mt-4">
            <strong>The measurement that decides everything:</strong> the collector logs, for every
            score change, the delay from feed report to first book move. If the book consistently
            moves first there is no event-reaction edge and strategies must come from model
            disagreement instead. That report is on each league&apos;s sports page.
          </p>
        </Card>
      </Section>

      {/* ── Live trading flow ── */}
      <Section title="Live Trading Flow">
        <Card><DataFlowDiagram /></Card>
      </Section>

      {/* ── Strategies ── */}
      <Section title="Strategies">
        <div className="overflow-x-auto">
          <table className="data-table w-full text-sm">
            <thead>
              <tr>
                <th className="text-left px-3 py-2 text-dim text-xs font-semibold uppercase border-b border-border">Name</th>
                <th className="text-left px-3 py-2 text-dim text-xs font-semibold uppercase border-b border-border">Ver</th>
                <th className="text-left px-3 py-2 text-dim text-xs font-semibold uppercase border-b border-border">Type</th>
                <th className="text-left px-3 py-2 text-dim text-xs font-semibold uppercase border-b border-border">Key Logic</th>
              </tr>
            </thead>
            <tbody>
              {[
                ["PoissonKelly", "11", "Jump-based", "Poisson jump counting, Kelly sizing, edge-based entry"],
                ["PoissonKellyDynamic", "7", "Adaptive", "Vol-scaled jump_bps, trend bias, dynamic thresholds"],
                ["DriftSniper", "9", "Momentum", "OLS velocity confirmation, 50%+ elapsed, requires full session"],
                ["GapFader", "7", "Mean reversion", "Dip entry at 0.50, TP 45% / SL 35%, vol boost"],
                ["RegimeSniper", "7", "Drift + filter", "Consistency window filter, sliding max_price"],
                ["TrendDrift", "2", "Regime-gated", "Medium/High vol + Trending only, late entry"],
                ["MeanRevFader", "2", "Regime-gated", "Flat trend only, small take-profits (15%)"],
                ["LateSniperCB", "1", "Regime-gated + CB", "Medium/High vol, late entry, circuit breaker"],
              ].map(([name, ver, type, desc]) => (
                <tr key={name} className="border-b border-border">
                  <td className="px-3 py-2 font-mono text-accent text-xs">{name}</td>
                  <td className="px-3 py-2 text-dim">{ver}</td>
                  <td className="px-3 py-2 text-dim">{type}</td>
                  <td className="px-3 py-2 text-dim text-xs">{desc}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      {/* ── IPC ── */}
      <Section title="Inter-Process Communication">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Card>
            <h3 className="text-sm font-semibold text-white mb-2">Unix Domain Socket</h3>
            <code className="text-xs text-[#39d2c0]">data/markets/&#123;slug&#125;/tick.sock</code>
            <p className="text-xs text-dim mt-2">
              Collector → Bot Workers. JSON-line protocol.<br />
              Messages: SessionStart, Tick, SessionEnd, Waiting.<br />
              Mid-session replay for late joiners.
            </p>
          </Card>
          <Card>
            <h3 className="text-sm font-semibold text-white mb-2">HTTP APIs</h3>
            <div className="text-xs space-y-1">
              <div><span className="text-btc font-mono">:8420</span> <span className="text-dim">— Orchestrator control (optional)</span></div>
              <div><span className="text-btc font-mono">:8421</span> <span className="text-dim">— CLOB bridge (order execution)</span></div>
              <div><span className="text-btc font-mono">:8422</span> <span className="text-dim">— Model server (sports rate params)</span></div>
              <div><span className="text-btc font-mono">:3456</span> <span className="text-dim">— Dashboard (REST + SSE + tRPC)</span></div>
              <div><span className="text-btc font-mono">:8080</span> <span className="text-dim">— Claim server (web UI)</span></div>
            </div>
          </Card>
          <Card>
            <h3 className="text-sm font-semibold text-white mb-2">File-Based State</h3>
            <div className="text-xs text-dim space-y-1 font-mono">
              <div><span className="text-[#39d2c0]">data/app.json</span> — orchestrator port + PID</div>
              <div><span className="text-[#39d2c0]">collector_state.json</span> — collector status</div>
              <div><span className="text-[#39d2c0]">bot_positions/&#123;strat&#125;.json</span> — worker state</div>
              <div><span className="text-[#39d2c0]">/tmp/fondaco-*-.pid</span> — PID files</div>
              <div><span className="text-[#39d2c0]">markets/&#123;league&#125;/games/&#123;game&#125;/</span> — sports, append-only</div>
              <div><span className="text-[#39d2c0]">markets/&#123;league&#125;/latency.jsonl</span> — feed latency</div>
            </div>
          </Card>
          <Card>
            <h3 className="text-sm font-semibold text-white mb-2">External WebSocket</h3>
            <code className="text-xs text-[#39d2c0]">wss://ws-live-data.polymarket.com</code>
            <p className="text-xs text-dim mt-2">
              Chainlink BTC/USD price feed. Collector subscribes with watchdog + auto-reconnect.
              Fallback: Coinbase → CoinGecko HTTP.
            </p>
          </Card>
        </div>
      </Section>

      {/* ── Rust modules ── */}
      <Section title="Rust Library Modules">
        <div className="overflow-x-auto">
          <table className="data-table w-full text-sm">
            <thead>
              <tr>
                <th className="text-left px-3 py-2 text-dim text-xs font-semibold uppercase border-b border-border">Module</th>
                <th className="text-left px-3 py-2 text-dim text-xs font-semibold uppercase border-b border-border">Purpose</th>
              </tr>
            </thead>
            <tbody>
              {[
                ["strategy.rs", "Strategy trait, Param/ParamValue system, helpers (held_side, check_exit, check_timing_guards)"],
                ["trader.rs", "TradeAction, Position, TickContext, SessionMetadata, SimExecutor, Trader"],
                ["engine.rs", "Simulator, session loading, load_session_summaries (with sidecar generation), Kelly sizing"],
                ["models.rs", "Sample, MarketMetadata, SessionData, SessionSummary"],
                ["defaults.rs", "API URLs, fee rates, token precision, path conventions, constants"],
                ["markets.rs", "Per-market config: window duration, max gap, poll interval"],
                ["tick_broadcast.rs", "TickBroadcaster (Unix socket server) + TickSubscriber (client), TickMessage protocol"],
                ["session_cache.rs", "SessionStatsCache: bisect-based prior(), gap_before(), data_density()"],
                ["session_io.rs", "Session JSON read/write, .summary.json sidecars, live session, PID files"],
                ["rtds.rs", "RtdsClient: WebSocket to Chainlink RTDS for live BTC price, watchdog + reconnect"],
                ["price_fetcher.rs", "take_sample: BTC price (RTDS→Coinbase→CoinGecko) + CLOB token midpoints/book"],
                ["market_discovery.rs", "Gamma API queries, event slug generation, fetch_metadata_with_retry"],
                ["volatility.rs", "Vol regime (Low/Medium/High), trend regime (Trending/MeanReverting/Flat) classifiers"],
                ["math_utils.rs", "norm_cdf, ols_slope_bps, ols_slope_bps_timed"],
                ["window_ops.rs", "wait_for_next_window, predict_next_window: timing for 5m/15m boundaries"],
              ].map(([mod, purpose]) => (
                <tr key={mod} className="border-b border-border">
                  <td className="px-3 py-2 font-mono text-accent text-xs whitespace-nowrap">{mod}</td>
                  <td className="px-3 py-2 text-dim text-xs">{purpose}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      {/* ── Orchestrator API ── */}
      <Section title="Orchestrator API">
        <p className="text-xs text-dim mb-3">
          Port <span className="font-mono text-btc">:8420</span>. The orchestrator is optional — collectors and workers can run standalone via CLI.
          The dashboard proxies to this API when available, falls back to file-based status when not.
        </p>
        <div className="overflow-x-auto">
          <table className="data-table w-full text-sm">
            <thead>
              <tr>
                <th className="text-left px-3 py-2 text-dim text-xs font-semibold uppercase border-b border-border">Method</th>
                <th className="text-left px-3 py-2 text-dim text-xs font-semibold uppercase border-b border-border">Path</th>
                <th className="text-left px-3 py-2 text-dim text-xs font-semibold uppercase border-b border-border">Action</th>
              </tr>
            </thead>
            <tbody>
              {[
                ["GET", "/status", "Markets, collectors, workers, clob-bridge health"],
                ["POST", "/market-on", "Start collector for a market"],
                ["POST", "/market-off", "Stop collector (SIGTERM or SIGUSR1 for graceful)"],
                ["POST", "/trade-on", "Start bot-worker (validates: clob-bridge + collector + tick.sock)"],
                ["POST", "/trade-off", "Stop bot-worker(s) by strategy or PID"],
                ["POST", "/set-cash", "Write pending bankroll to worker state file"],
                ["POST", "/reload", "Kill + respawn worker with current bankroll"],
                ["POST", "/stop", "Stop orchestrator only (children keep running)"],
                ["POST", "/stop-all", "Graceful stop orchestrator + all children"],
                ["POST", "/kill-all", "Force kill everything"],
              ].map(([method, path, desc]) => (
                <tr key={path} className="border-b border-border">
                  <td className="px-3 py-2 text-xs font-mono" style={{ color: method === "GET" ? "#58a6ff" : "#3fb950" }}>{method}</td>
                  <td className="px-3 py-2 font-mono text-xs text-white">{path}</td>
                  <td className="px-3 py-2 text-dim text-xs">{desc}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      {/* ── Data directory ── */}
      <Section title="Data Directory">
        <Card>
          <pre className="text-xs font-mono text-dim leading-relaxed whitespace-pre">{`data/
├── app.json                                    orchestrator port, PID, started_at
├── markets/
│   ├── btc-updown-5m/
│   │   ├── sessions/
│   │   │   ├── 2026-03-01T12-00-00Z.json           full session (metadata + samples + outcome)
│   │   │   ├── 2026-03-01T12-00-00Z.summary.json   lightweight sidecar (~100 bytes)
│   │   │   └── *.trading.{strategy}.json            trade sidecar
│   │   ├── live/
│   │   │   └── session.json                         current session or "waiting" status
│   │   ├── bot_positions/
│   │   │   └── {Strategy}.json                      worker state: cash, tokens, bankroll
│   │   ├── bot_trades.jsonl                         all trades (append-only)
│   │   ├── bot_log.jsonl                            session results (append-only)
│   │   ├── collector_state.json                     collector PID, session count, state
│   │   └── tick.sock                                Unix domain socket (collector → workers)
│   └── btc-updown-15m/
│       └── ...                                      (same structure)
└── optimizer/
    └── config.json                                  backtest sweep configuration`}</pre>
        </Card>
      </Section>

      {/* ── Key constants ── */}
      <Section title="Key Constants">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Card>
            <h3 className="text-sm font-semibold text-white mb-2">Trading</h3>
            <div className="text-xs text-dim space-y-1">
              <div>Poll interval: <span className="text-white font-mono">~55ms</span></div>
              <div>Price decimals: <span className="text-white font-mono">2</span></div>
              <div>Token decimals: <span className="text-white font-mono">4</span></div>
              <div>Min order size: <span className="text-white font-mono">5 tokens</span></div>
              <div>Default expiration: <span className="text-white font-mono">120s</span></div>
              <div>Taker fee rate: <span className="text-white font-mono">25% of spread</span></div>
              <div>Simulated latency: <span className="text-white font-mono">200ms</span></div>
            </div>
          </Card>
          <Card>
            <h3 className="text-sm font-semibold text-white mb-2">Markets</h3>
            <div className="text-xs text-dim space-y-1">
              <div>BTC 5m: <span className="text-white font-mono">300s window, 600s max gap, ~55ms poll</span></div>
              <div>BTC 15m: <span className="text-white font-mono">900s window, 1800s max gap</span></div>
            </div>
          </Card>
        </div>
      </Section>

      <footer className="text-center text-dim text-xs py-6 border-t border-border">
        Generated from codebase state. If architecture changes, update this page and CLAUDE.md.
      </footer>
    </div>
  );
}
