// Command backtest runs grid search backtesting of trading strategies against
// historical Polymarket session data. Ported from Python simulator/backtest.py.
//
// Usage:
//
//	go run ./cmd/backtest btc-updown-5m --strategy all --top 10 --recent 30
//	go run ./cmd/backtest data/markets/btc-updown-5m/sessions --strategy LateSniper
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
	"github.com/polymarket/bitcoin-5-minute/internal/simulator"
	"github.com/polymarket/bitcoin-5-minute/internal/strategy"
)

func main() {
	// Define flags
	strategyFlag := flag.String("strategy", "all", "Strategy name or 'all'")
	optimizeFlag := flag.String("optimize", "sharpe", "Metric to optimize: sharpe, total_pnl, win_rate")
	executionFlag := flag.String("execution", "book", "Execution mode: book, midpoint, midpoint_slippage")
	slippageFlag := flag.Float64("slippage", 0.02, "Half-spread for midpoint_slippage mode")
	cashFlag := flag.String("cash", "100", `Starting cash per session. Use "cached" to load from .cache/balance.json`)
	topFlag := flag.Int("top", 10, "Show top N parameter combos")
	recentFlag := flag.Int("recent", 0, "Only use the N most recent sessions (0 = all)")
	verboseFlag := flag.Bool("verbose", false, "Per-session breakdown for best params")

	flag.Parse()

	// First non-flag arg is data_dir
	args := flag.Args()
	if len(args) < 1 {
		fmt.Fprintln(os.Stderr, "Usage: backtest <data_dir> [flags]")
		fmt.Fprintln(os.Stderr, "  data_dir: path to session files or slug key (e.g. btc-updown-5m)")
		os.Exit(1)
	}
	rawDataDir := args[0]

	// Validate optimize flag
	validOptimize := map[string]bool{"sharpe": true, "total_pnl": true, "win_rate": true}
	if !validOptimize[*optimizeFlag] {
		fmt.Fprintf(os.Stderr, "Invalid --optimize value: %s. Choose: sharpe, total_pnl, win_rate\n", *optimizeFlag)
		os.Exit(1)
	}

	// Resolve starting cash
	var startingCash float64
	if *cashFlag == "cached" {
		cash, err := loadCachedBalance()
		if err != nil {
			fmt.Fprintf(os.Stderr, "No cached balance found: %v\nUse a numeric value or run the bot first.\n", err)
			os.Exit(1)
		}
		startingCash = cash
		fmt.Printf("Loaded cached balance: $%.2f\n", startingCash)
	} else {
		var err error
		_, err = fmt.Sscanf(*cashFlag, "%f", &startingCash)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Invalid --cash value: %s\n", *cashFlag)
			os.Exit(1)
		}
	}

	// Resolve data dir (supports slug key shorthand)
	dataDir := resolveDataDir(rawDataDir)
	backtestLogFile := resolveBacktestLog(dataDir)

	// Load sessions
	allSessions, err := simulator.LoadSessions(dataDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error loading sessions: %v\n", err)
		os.Exit(1)
	}
	if len(allSessions) == 0 {
		fmt.Fprintf(os.Stderr, "No resolved sessions found in: %s\n", dataDir)
		os.Exit(1)
	}

	sessions := allSessions
	if *recentFlag > 0 && *recentFlag < len(allSessions) {
		sessions = allSessions[len(allSessions)-*recentFlag:]
	}

	fmt.Printf("Loaded %d resolved sessions from %s\n", len(sessions), dataDir)
	fmt.Printf("Execution: %s (slippage=%.2f)  Starting cash: $%.0f\n",
		*executionFlag, *slippageFlag, startingCash)

	// Select strategies
	var strategyNames []string
	if *strategyFlag == "all" {
		for name := range strategy.AllStrategies {
			strategyNames = append(strategyNames, name)
		}
		sort.Strings(strategyNames)
	} else {
		if _, ok := strategy.AllStrategies[*strategyFlag]; !ok {
			names := make([]string, 0, len(strategy.AllStrategies))
			for n := range strategy.AllStrategies {
				names = append(names, n)
			}
			sort.Strings(names)
			fmt.Fprintf(os.Stderr, "Unknown strategy: %s. Available: %s\n",
				*strategyFlag, strings.Join(names, ", "))
			os.Exit(1)
		}
		strategyNames = []string{*strategyFlag}
	}

	// Build all strategy variants
	var allVariants []variantEntry
	for _, name := range strategyNames {
		factory := strategy.AllStrategies[name]
		variants := buildVariants(factory)
		allVariants = append(allVariants, variants...)
	}

	totalVariants := len(allVariants)

	// Run grid search with goroutine worker pool
	var allResults []simulator.BacktestResult

	if totalVariants <= 1 {
		// Single variant: run directly
		sim := simulator.NewSimulator(startingCash, *executionFlag, *slippageFlag, allSessions)
		for _, v := range allVariants {
			result := sim.RunBacktest(v.strat, sessions)
			result.Params = v.params
			allResults = append(allResults, result)
		}
	} else {
		numWorkers := runtime.NumCPU()
		if numWorkers > totalVariants {
			numWorkers = totalVariants
		}
		fmt.Printf("Running %d variants across %d workers...\n", totalVariants, numWorkers)

		type workItem struct {
			index int
			entry variantEntry
		}

		results := make([]simulator.BacktestResult, totalVariants)
		work := make(chan workItem, totalVariants)
		var wg sync.WaitGroup

		for w := 0; w < numWorkers; w++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				// Each goroutine gets its own Simulator (strategies are not
				// goroutine-safe since they hold mutable state)
				sim := simulator.NewSimulator(startingCash, *executionFlag, *slippageFlag, allSessions)
				for item := range work {
					result := sim.RunBacktest(item.entry.strat, sessions)
					result.Params = item.entry.params
					results[item.index] = result
				}
			}()
		}

		for i, v := range allVariants {
			work <- workItem{index: i, entry: v}
		}
		close(work)
		wg.Wait()

		allResults = results[:]
	}

	fmt.Printf("Tested %d strategy variants across %d strategies\n",
		totalVariants, len(strategyNames))

	// Output
	printLeaderboard(allResults, *topFlag, *optimizeFlag)
	printStrategySummary(allResults, *optimizeFlag)

	if *verboseFlag {
		sortKey := getSortKey(*optimizeFlag)
		best := allResults[0]
		for _, r := range allResults[1:] {
			if sortKey(r) > sortKey(best) {
				best = r
			}
		}
		printVerbose(best)
	}

	// Persist full run to JSONL log
	logBacktestRun(rawDataDir, *strategyFlag, *executionFlag, *slippageFlag,
		startingCash, *optimizeFlag, len(sessions), totalVariants,
		allResults, backtestLogFile)
	fmt.Printf("Results logged to %s\n", backtestLogFile)
}

// ---------------------------------------------------------------------------
// Strategy variant building (param grid cartesian product)
// ---------------------------------------------------------------------------

type variantEntry struct {
	strat  strategy.Strategy
	params map[string]interface{}
}

func buildVariants(factory strategy.StrategyFactory) []variantEntry {
	// Create a default instance to read the param grid
	defaultStrat := factory(nil)
	grid := defaultStrat.ParamGrid()

	if len(grid) == 0 {
		params := getStratParams(defaultStrat)
		return []variantEntry{{strat: defaultStrat, params: params}}
	}

	// Sort keys for deterministic ordering
	keys := make([]string, 0, len(grid))
	for k := range grid {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	values := make([][]interface{}, len(keys))
	for i, k := range keys {
		values[i] = grid[k]
	}

	// Cartesian product
	combos := cartesianProduct(values)
	variants := make([]variantEntry, 0, len(combos))
	for _, combo := range combos {
		params := make(map[string]interface{}, len(keys))
		for i, k := range keys {
			params[k] = combo[i]
		}
		s := factory(params)
		variants = append(variants, variantEntry{strat: s, params: params})
	}
	return variants
}

func cartesianProduct(arrays [][]interface{}) [][]interface{} {
	if len(arrays) == 0 {
		return [][]interface{}{nil}
	}

	result := [][]interface{}{nil}
	for _, arr := range arrays {
		var newResult [][]interface{}
		for _, prev := range result {
			for _, val := range arr {
				combo := make([]interface{}, len(prev)+1)
				copy(combo, prev)
				combo[len(prev)] = val
				newResult = append(newResult, combo)
			}
		}
		result = newResult
	}
	return result
}

func getStratParams(s strategy.Strategy) map[string]interface{} {
	if ps, ok := s.(interface{ Params() map[string]interface{} }); ok {
		p := ps.Params()
		if p != nil {
			return p
		}
	}
	return make(map[string]interface{})
}

// ---------------------------------------------------------------------------
// Output formatting
// ---------------------------------------------------------------------------

func formatParams(params map[string]interface{}) string {
	if len(params) == 0 {
		return "-"
	}
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		v := params[k]
		switch val := v.(type) {
		case float64:
			parts = append(parts, fmt.Sprintf("%s=%.*g", k, 3, val))
		case float32:
			parts = append(parts, fmt.Sprintf("%s=%.*g", k, 3, float64(val)))
		default:
			parts = append(parts, fmt.Sprintf("%s=%v", k, v))
		}
	}
	return strings.Join(parts, ", ")
}

func getSortKey(optimize string) func(simulator.BacktestResult) float64 {
	switch optimize {
	case "total_pnl":
		return func(r simulator.BacktestResult) float64 { return r.TotalPnL }
	case "win_rate":
		return func(r simulator.BacktestResult) float64 { return r.WinRate }
	default: // sharpe
		return func(r simulator.BacktestResult) float64 { return r.Sharpe }
	}
}

func printLeaderboard(results []simulator.BacktestResult, topN int, optimize string) {
	sortKey := getSortKey(optimize)

	sort.Slice(results, func(i, j int) bool {
		return sortKey(results[i]) > sortKey(results[j])
	})

	shown := results
	if topN > 0 && topN < len(shown) {
		shown = shown[:topN]
	}

	// Header
	fmt.Printf("\n%-20s %8s %10s %10s %8s %6s  %s\n",
		"Strategy", "Sharpe", "Total PnL", "Mean PnL", "Win%", "N", "Params")
	fmt.Println(strings.Repeat("-", 95))

	for _, r := range shown {
		fmt.Printf("%-20s %8.3f %10.2f %10.2f %7.1f%% %6d  %s\n",
			r.StrategyName,
			r.Sharpe,
			r.TotalPnL,
			r.MeanPnL,
			r.WinRate*100,
			r.NumSessions,
			formatParams(r.Params),
		)
	}
	fmt.Println()
}

func printStrategySummary(results []simulator.BacktestResult, optimize string) {
	sortKey := getSortKey(optimize)

	// Group by strategy name
	byStrategy := make(map[string][]simulator.BacktestResult)
	for _, r := range results {
		byStrategy[r.StrategyName] = append(byStrategy[r.StrategyName], r)
	}

	fmt.Println(strings.Repeat("=", 70))
	fmt.Printf("BEST PARAMS PER STRATEGY (optimizing: %s)\n", optimize)
	fmt.Println(strings.Repeat("=", 70))

	names := make([]string, 0, len(byStrategy))
	for name := range byStrategy {
		names = append(names, name)
	}
	sort.Strings(names)

	for _, name := range names {
		variants := byStrategy[name]
		best := variants[0]
		for _, v := range variants[1:] {
			if sortKey(v) > sortKey(best) {
				best = v
			}
		}
		fmt.Printf("\n  %s (%d variants tested)\n", name, len(variants))
		fmt.Printf("    Best:  Sharpe=%.3f  PnL=%.2f  Win=%.1f%%\n",
			best.Sharpe, best.TotalPnL, best.WinRate*100)
		fmt.Printf("    Params: %s\n", formatParams(best.Params))
	}
	fmt.Println()
}

func printVerbose(result simulator.BacktestResult) {
	fmt.Printf("\n--- %s [%s] ---\n", result.StrategyName, formatParams(result.Params))
	for _, sr := range result.Sessions {
		var tradeParts []string
		for _, t := range sr.Trades {
			tradeParts = append(tradeParts, fmt.Sprintf("%s@%.3f", t.Side, t.Price))
		}
		tradeStr := ""
		if len(tradeParts) > 0 {
			tradeStr = " | " + strings.Join(tradeParts, ", ")
		}

		sessionName := "?"
		if sr.SessionFile != "" {
			sessionName = filepath.Base(sr.SessionFile)
		}
		fmt.Printf("  %s outcome=%-4s pnl=%+7.2f trades=%-2d%s\n",
			sessionName, sr.Outcome, sr.PnL, len(sr.Trades), tradeStr)
	}
	fmt.Printf("  TOTAL: pnl=%+.2f  mean=%+.2f  win_rate=%.1f%%  sharpe=%.3f\n",
		result.TotalPnL, result.MeanPnL, result.WinRate*100, result.Sharpe)
}

// ---------------------------------------------------------------------------
// Data dir and path resolution
// ---------------------------------------------------------------------------

func resolveDataDir(rawPath string) string {
	// If path exists as-is, use it
	if info, err := os.Stat(rawPath); err == nil && info.IsDir() {
		return rawPath
	}
	// Try as slug key -> data/markets/{slug_key}/sessions
	slugKey := strings.TrimRight(rawPath, "-")
	marketPath := core.MarketSessionsDir(slugKey)
	if info, err := os.Stat(marketPath); err == nil && info.IsDir() {
		return marketPath
	}
	// Return as-is (will fail with a clear error later)
	return rawPath
}

func resolveBacktestLog(dataDir string) string {
	marketsDir := filepath.Clean(core.MarketsDir())
	normDir := filepath.Clean(dataDir)
	if strings.HasPrefix(normDir, marketsDir+string(filepath.Separator)) {
		rel, err := filepath.Rel(marketsDir, normDir)
		if err == nil {
			parts := strings.SplitN(rel, string(filepath.Separator), 2)
			slugKey := parts[0]
			return core.MarketBacktestLog(slugKey)
		}
	}
	// Fallback: global backtest log
	return filepath.Join(core.BaseDir, "data", "backtest_log.jsonl")
}

func loadCachedBalance() (float64, error) {
	data, err := os.ReadFile(core.BalanceCacheFile())
	if err != nil {
		return 0, err
	}
	var cached map[string]interface{}
	if err := json.Unmarshal(data, &cached); err != nil {
		return 0, err
	}
	cash, ok := cached["cash"].(float64)
	if !ok {
		return 0, fmt.Errorf("no 'cash' field in balance cache")
	}
	return cash, nil
}

// ---------------------------------------------------------------------------
// Backtest logging
// ---------------------------------------------------------------------------

func logBacktestRun(dataDir, strategyName, execution string, slippage, startingCash float64,
	optimize string, numSessions, totalVariants int,
	allResults []simulator.BacktestResult, logFile string) {

	sortKey := getSortKey(optimize)

	// Best params per strategy
	byStrategy := make(map[string][]simulator.BacktestResult)
	for _, r := range allResults {
		byStrategy[r.StrategyName] = append(byStrategy[r.StrategyName], r)
	}

	var bestPerStrategy []interface{}
	names := make([]string, 0, len(byStrategy))
	for name := range byStrategy {
		names = append(names, name)
	}
	sort.Strings(names)

	for _, name := range names {
		variants := byStrategy[name]
		best := variants[0]
		for _, v := range variants[1:] {
			if sortKey(v) > sortKey(best) {
				best = v
			}
		}
		bestPerStrategy = append(bestPerStrategy, map[string]interface{}{
			"strategy":        best.StrategyName,
			"params":          best.Params,
			"variants_tested": len(variants),
			"sharpe":          round4(best.Sharpe),
			"total_pnl":       round4(best.TotalPnL),
			"mean_pnl":        round4(best.MeanPnL),
			"win_rate":        round4(best.WinRate),
			"num_sessions":    best.NumSessions,
		})
	}

	entry := map[string]interface{}{
		"timestamp": time.Now().UTC().Format(time.RFC3339),
		"inputs": map[string]interface{}{
			"data_dir":       dataDir,
			"strategy":       strategyName,
			"execution":      execution,
			"slippage":       slippage,
			"starting_cash":  startingCash,
			"optimize":       optimize,
			"num_sessions":   numSessions,
			"total_variants": totalVariants,
		},
		"best_per_strategy": bestPerStrategy,
	}

	// Ensure directory exists
	dir := filepath.Dir(logFile)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		log.Printf("Warning: failed to create log directory: %v", err)
		return
	}

	data, err := json.Marshal(entry)
	if err != nil {
		log.Printf("Warning: failed to marshal backtest log: %v", err)
		return
	}

	f, err := os.OpenFile(logFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		log.Printf("Warning: failed to write backtest log: %v", err)
		return
	}
	defer f.Close()
	f.Write(data)
	f.Write([]byte("\n"))
}

func round4(v float64) float64 {
	return math.Round(v*10000) / 10000
}
