// Package simulator provides the backtesting engine for replaying trading
// strategies against historical session data. Ported from Python
// simulator/engine.py.
package simulator

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
	"github.com/polymarket/bitcoin-5-minute/internal/strategy"
)

// ---------------------------------------------------------------------------
// Adapter: strategy.Strategy -> core.Strategy
// ---------------------------------------------------------------------------

// strategyAdapter wraps a strategy.Strategy to satisfy core.Strategy.
// core.Strategy expects OnTick(*TickContext) and TickLogs(), while
// strategy.Strategy has OnTick(TickContext) and DrainTickLogs().
type strategyAdapter struct {
	inner strategy.Strategy
}

func (a *strategyAdapter) Name() string { return a.inner.Name() }

func (a *strategyAdapter) OnSessionStart(metadata map[string]interface{}) {
	a.inner.OnSessionStart(metadata)
}

func (a *strategyAdapter) OnTick(ctx *core.TickContext) core.TradeAction {
	return a.inner.OnTick(*ctx)
}

func (a *strategyAdapter) TickLogs() []string {
	return a.inner.DrainTickLogs()
}

// wrapStrategy wraps a strategy.Strategy into a core.Strategy.
func wrapStrategy(s strategy.Strategy) core.Strategy {
	return &strategyAdapter{inner: s}
}

// ---------------------------------------------------------------------------
// Result types
// ---------------------------------------------------------------------------

// SessionResult holds the result of replaying one session.
type SessionResult struct {
	SessionFile   string
	Outcome       string
	PnL           float64
	PnLPct        float64
	Trades        []core.Trade
	FinalPosition core.Position
	NumSamples    int
}

// BacktestResult holds aggregate results across all sessions for one
// strategy+param combination.
type BacktestResult struct {
	StrategyName string
	Params       map[string]interface{}
	Sessions     []SessionResult
	TotalPnL     float64
	MeanPnL      float64
	WinRate      float64
	Sharpe       float64
	NumSessions  int
}

// ---------------------------------------------------------------------------
// Session loading
// ---------------------------------------------------------------------------

// LoadSessions loads all resolved session JSON files from a directory tree.
// Files without an "outcome" or "samples" key are skipped. Files with
// ".trading." in the name are also skipped. Returns sessions sorted by
// filename.
func LoadSessions(dataDir string) ([]map[string]interface{}, error) {
	var files []string

	// Recursive glob for *.json
	err := filepath.Walk(dataDir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return nil // skip inaccessible paths
		}
		if !info.IsDir() && strings.HasSuffix(strings.ToLower(info.Name()), ".json") {
			files = append(files, path)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}

	sort.Strings(files)

	var sessions []map[string]interface{}
	for _, f := range files {
		// Skip trading state files
		if strings.Contains(filepath.Base(f), ".trading.") {
			continue
		}

		data, err := os.ReadFile(f)
		if err != nil {
			continue
		}

		var session map[string]interface{}
		if err := json.Unmarshal(data, &session); err != nil {
			continue
		}

		// Skip unresolved sessions (outcome is nil/missing) and non-session files
		outcome, hasOutcome := session["outcome"]
		if !hasOutcome || outcome == nil {
			continue
		}
		if _, hasSamples := session["samples"]; !hasSamples {
			continue
		}

		session["_source_file"] = f
		sessions = append(sessions, session)
	}

	return sessions, nil
}

// ---------------------------------------------------------------------------
// Session summaries (for strategies that need lookback)
// ---------------------------------------------------------------------------

// sessionSummary is an internal struct for sorted session lookback data.
type sessionSummary struct {
	WindowStart time.Time
	InitialBtc  float64
	FinalBtc    float64
	Outcome     string
}

// buildSessionSummaries parses sessions into sorted summaries.
func buildSessionSummaries(sessions []map[string]interface{}) []map[string]interface{} {
	var summaries []sessionSummary
	for _, s := range sessions {
		meta, ok := s["metadata"].(map[string]interface{})
		if !ok {
			continue
		}
		ws, _ := meta["window_start_utc"].(string)
		windowStart, err := parseUTC(ws)
		if err != nil {
			continue
		}

		initialBtc, ok1 := toFloat64(s["initial_btc_price_usd"])
		finalBtc, ok2 := toFloat64(s["final_btc_price_usd"])
		outcome, ok3 := s["outcome"].(string)
		if !ok1 || !ok2 || !ok3 || initialBtc == 0 || finalBtc == 0 {
			continue
		}

		summaries = append(summaries, sessionSummary{
			WindowStart: windowStart,
			InitialBtc:  initialBtc,
			FinalBtc:    finalBtc,
			Outcome:     outcome,
		})
	}

	sort.Slice(summaries, func(i, j int) bool {
		return summaries[i].WindowStart.Before(summaries[j].WindowStart)
	})

	result := make([]map[string]interface{}, len(summaries))
	for i, s := range summaries {
		result[i] = map[string]interface{}{
			"window_start": s.WindowStart,
			"initial_btc":  s.InitialBtc,
			"final_btc":    s.FinalBtc,
			"outcome":      s.Outcome,
		}
	}
	return result
}

// parseUTC parses a UTC timestamp string (ISO 8601 with optional Z suffix).
func parseUTC(s string) (time.Time, error) {
	if s == "" {
		return time.Time{}, fmt.Errorf("empty timestamp")
	}
	if strings.HasSuffix(s, "Z") {
		s = s[:len(s)-1] + "+00:00"
	}
	// Try common formats
	for _, layout := range []string{
		"2006-01-02T15:04:05.999999-07:00",
		"2006-01-02T15:04:05-07:00",
		"2006-01-02T15:04:05.999999Z07:00",
		"2006-01-02T15:04:05Z07:00",
		"2006-01-02T15:04:05.999999",
		"2006-01-02T15:04:05",
		time.RFC3339,
		time.RFC3339Nano,
	} {
		if t, err := time.Parse(layout, s); err == nil {
			return t.UTC(), nil
		}
	}
	return time.Time{}, fmt.Errorf("cannot parse timestamp: %s", s)
}

// toFloat64 converts a JSON numeric value to float64.
func toFloat64(v interface{}) (float64, bool) {
	if v == nil {
		return 0, false
	}
	switch val := v.(type) {
	case float64:
		return val, val != 0
	case int:
		return float64(val), val != 0
	case int64:
		return float64(val), val != 0
	case json.Number:
		f, err := val.Float64()
		return f, err == nil && f != 0
	}
	return 0, false
}

// ---------------------------------------------------------------------------
// Simulator
// ---------------------------------------------------------------------------

// Simulator replays sessions through strategies for backtesting.
type Simulator struct {
	StartingCash     float64
	ExecutionMode    string
	Slippage         float64
	sessionSummaries []map[string]interface{}
}

// NewSimulator creates a Simulator. Pass allSessions to enable prior-session
// lookback for strategies that need it.
func NewSimulator(startingCash float64, executionMode string, slippage float64,
	allSessions []map[string]interface{}) *Simulator {

	var summaries []map[string]interface{}
	if len(allSessions) > 0 {
		summaries = buildSessionSummaries(allSessions)
	}

	return &Simulator{
		StartingCash:     startingCash,
		ExecutionMode:    executionMode,
		Slippage:         slippage,
		sessionSummaries: summaries,
	}
}

// RunSession replays one session through a strategy.
func (s *Simulator) RunSession(strat strategy.Strategy, sessionDict map[string]interface{}) SessionResult {
	samplesRaw, _ := sessionDict["samples"].([]interface{})
	metadataRaw, _ := sessionDict["metadata"].(map[string]interface{})
	outcomeStr, _ := sessionDict["outcome"].(string)
	sourceFile, _ := sessionDict["_source_file"].(string)

	initialBtc := 0.0
	if v, ok := toFloat64(sessionDict["initial_btc_price_usd"]); ok {
		initialBtc = v
	}

	// Build metadata copy with prior session summaries injected
	metadata := make(map[string]interface{}, len(metadataRaw)+1)
	for k, v := range metadataRaw {
		metadata[k] = v
	}

	if s.sessionSummaries != nil {
		ws, _ := metadata["window_start_utc"].(string)
		if currentStart, err := parseUTC(ws); err == nil {
			var prior []map[string]interface{}
			for _, sum := range s.sessionSummaries {
				if ws, ok := sum["window_start"].(time.Time); ok && ws.Before(currentStart) {
					prior = append(prior, sum)
				}
			}
			metadata["_prior_session_summaries"] = prior
		}
	}

	// Convert samples from []interface{} to []map[string]interface{}
	samples := make([]map[string]interface{}, 0, len(samplesRaw))
	for _, raw := range samplesRaw {
		if m, ok := raw.(map[string]interface{}); ok {
			samples = append(samples, m)
		}
	}

	// Compute window duration from samples
	windowDuration := 300.0
	if len(samples) >= 2 {
		if es, ok := samples[len(samples)-1]["elapsed_seconds"]; ok {
			if v, ok2 := toFloat64(es); ok2 {
				windowDuration = v
			}
		}
	}

	position := &core.Position{Cash: s.StartingCash}
	executor := core.NewSimExecutor(s.ExecutionMode, s.Slippage)
	// Wrap strategy.Strategy into core.Strategy for the Trader
	coreStrat := wrapStrategy(strat)
	trader := core.NewTrader(coreStrat, executor)
	trader.StartSession(metadata, position, initialBtc, windowDuration, s.StartingCash, false)

	for _, sample := range samples {
		trader.ProcessTick(sample)
	}

	trades := trader.EndSession(outcomeStr)

	pnl := position.Cash - s.StartingCash
	pnlPct := 0.0
	if s.StartingCash > 0 {
		pnlPct = (pnl / s.StartingCash) * 100.0
	}

	return SessionResult{
		SessionFile:   sourceFile,
		Outcome:       outcomeStr,
		PnL:           pnl,
		PnLPct:        pnlPct,
		Trades:        trades,
		FinalPosition: *position,
		NumSamples:    len(samples),
	}
}

// RunBacktest runs a strategy across all sessions and aggregates stats.
func (s *Simulator) RunBacktest(strat strategy.Strategy, sessions []map[string]interface{}) BacktestResult {
	results := make([]SessionResult, len(sessions))
	for i, sess := range sessions {
		results[i] = s.RunSession(strat, sess)
	}

	n := len(results)
	totalPnL := 0.0
	wins := 0
	for _, r := range results {
		totalPnL += r.PnL
		if r.PnL > 0 {
			wins++
		}
	}

	meanPnL := 0.0
	if n > 0 {
		meanPnL = totalPnL / float64(n)
	}

	winRate := 0.0
	if n > 0 {
		winRate = float64(wins) / float64(n)
	}

	// Sharpe: mean / std of per-session PnL (sample std dev)
	sharpe := 0.0
	if n > 1 {
		pnls := make([]float64, n)
		for i, r := range results {
			pnls[i] = r.PnL
		}
		variance := 0.0
		for _, p := range pnls {
			diff := p - meanPnL
			variance += diff * diff
		}
		variance /= float64(n - 1)
		std := math.Sqrt(variance)
		if std > 0 {
			sharpe = meanPnL / std
		}
	}

	// Get params from strategy (optional interface)
	var params map[string]interface{}
	if ps, ok := strat.(interface{ Params() map[string]interface{} }); ok {
		params = ps.Params()
	}
	if params == nil {
		params = make(map[string]interface{})
	}

	return BacktestResult{
		StrategyName: strat.Name(),
		Params:       params,
		Sessions:     results,
		TotalPnL:     totalPnL,
		MeanPnL:      meanPnL,
		WinRate:       winRate,
		Sharpe:        sharpe,
		NumSessions:  n,
	}
}
