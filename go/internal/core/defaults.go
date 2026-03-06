// Package core provides default constants, path helpers, and shared types
// for the Polymarket trading system. Ported from Python core/defaults.py.
package core

import (
	"os"
	"path/filepath"
	"strings"
)

// API endpoints.
const (
	GammaAPIBase    = "https://gamma-api.polymarket.com"
	ClobAPIBase     = "https://clob.polymarket.com"
	CoinbaseBtcURL  = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
	CoingeckoBtcURL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
)

// Timing constants.
const (
	PollIntervalSeconds      = 0.055
	WindowDurationSeconds    = 300
	ResolutionPollInterval   = 5.0
	ResolutionTimeoutSeconds = 300
)

// Collection.
const TargetSessions = 100

// Trading guardrails (shared by simulator and bot).
const ClobMinOrderTokens = 5

// App (central process).
const DefaultControlPort = 8420

// BaseDir is the project root directory. It checks the POLYMARKET_BASE_DIR
// environment variable first, then falls back to two levels above the
// directory containing this Go source (i.e., go/ -> project root).
// At runtime (compiled binary), it uses the executable location.
var BaseDir string

func init() {
	if env := os.Getenv("POLYMARKET_BASE_DIR"); env != "" {
		BaseDir = env
		return
	}
	// Try to resolve from the executable path: binary is expected
	// somewhere under go/ (e.g., go/bin/), so walk up to find the
	// project root that contains a "data" directory.
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		// Walk up at most 5 levels looking for data/ directory.
		for i := 0; i < 5; i++ {
			candidate := filepath.Dir(dir)
			if _, err := os.Stat(filepath.Join(candidate, "data")); err == nil {
				BaseDir = candidate
				return
			}
			dir = candidate
		}
	}
	// Final fallback: current working directory's parent (assume we're in go/).
	if cwd, err := os.Getwd(); err == nil {
		BaseDir = filepath.Dir(cwd)
	}
}

// Derived path constants (computed from BaseDir).

func APIRawDir() string        { return filepath.Join(BaseDir, "data", "api_raw") }
func BalanceCacheFile() string  { return filepath.Join(BaseDir, ".cache", "balance.json") }
func AppStateFile() string     { return filepath.Join(BaseDir, "data", "app.json") }
func AppLogFile() string       { return filepath.Join(BaseDir, "data", "app.log") }
func MarketsDir() string       { return filepath.Join(BaseDir, "data", "markets") }

// SlugKey normalizes a slug prefix to a directory key by stripping
// any trailing dash. E.g., "btc-updown-5m-" -> "btc-updown-5m".
func SlugKey(slugPrefix string) string {
	return strings.TrimRight(slugPrefix, "-")
}

// Per-market path helpers. All accept a slug key (already normalized).

func MarketDataDir(slugKey string) string {
	return filepath.Join(MarketsDir(), slugKey)
}

func MarketSessionsDir(slugKey string) string {
	return filepath.Join(MarketsDir(), slugKey, "sessions")
}

func MarketLiveDir(slugKey string) string {
	return filepath.Join(MarketsDir(), slugKey, "live")
}

func MarketSocketPath(slugKey string) string {
	return filepath.Join(MarketsDir(), slugKey, "ticks.sock")
}

func MarketWorkersDir(slugKey string) string {
	return filepath.Join(MarketsDir(), slugKey, "workers")
}

func MarketStrategiesFile(slugKey string) string {
	return filepath.Join(MarketsDir(), slugKey, "strategies.json")
}

func MarketTradesFile(slugKey string) string {
	return filepath.Join(MarketsDir(), slugKey, "bot_trades.jsonl")
}

func MarketSessionLog(slugKey string) string {
	return filepath.Join(MarketsDir(), slugKey, "bot_log.jsonl")
}

func MarketBacktestLog(slugKey string) string {
	return filepath.Join(MarketsDir(), slugKey, "backtest_log.jsonl")
}
