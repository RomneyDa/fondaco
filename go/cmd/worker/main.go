// cmd/worker/main.go
// Standalone worker binary entry point.
// Ported from Python app/bot_worker.py main().

package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/polymarket/bitcoin-5-minute/internal/app"
	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

func main() {
	strategyName := flag.String("strategy", "", "Strategy name (required)")
	paramsStr := flag.String("params", "", "Strategy params as k=v,k=v")
	socketPath := flag.String("socket", "", "Tick broadcast socket path (required)")
	marketDir := flag.String("market-dir", "", "Per-market data directory (required)")
	cash := flag.Float64("cash", 100.0, "Starting cash")
	dryRun := flag.Bool("dry-run", false, "Trade without real orders")
	sessionCapVal := flag.Float64("session-cap", 0, "Max dollars deployed per session (0 = no cap)")
	perSession := flag.Bool("per-session", false, "Reset cash at each session start")
	window := flag.Int("window", 300, "Window duration in seconds")
	restore := flag.Bool("restore", false, "Restore state from disk")

	flag.Parse()

	if *strategyName == "" || *socketPath == "" || *marketDir == "" {
		fmt.Println("Usage: worker --strategy NAME --socket PATH --market-dir DIR [options]")
		flag.PrintDefaults()
		os.Exit(1)
	}

	params := parseParams(*paramsStr)

	var sessionCap *float64
	if *sessionCapVal > 0 {
		sessionCap = sessionCapVal
	}

	// Set up logging.
	logDir := filepath.Join(*marketDir, "workers", *strategyName)
	os.MkdirAll(logDir, 0o755)

	logFile, err := os.OpenFile(filepath.Join(logDir, "worker.log"),
		os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		log.Fatalf("Failed to open worker log: %v", err)
	}
	defer logFile.Close()

	log.SetOutput(logFile)
	log.SetFlags(log.LstdFlags)

	// Create worker.
	worker, err := app.NewBotWorker(
		*strategyName,
		params,
		*socketPath,
		*cash,
		*marketDir,
		*dryRun,
		sessionCap,
		*perSession,
		*window,
	)
	if err != nil {
		log.Fatalf("Failed to create worker: %v", err)
	}

	// TODO: Set strategy based on strategyName.
	// This requires a strategy registry (not yet ported to Go).
	// For now, log an error if no strategy is available.
	_ = core.Hold // reference core package

	// Strategy registration would look like:
	//   strategy := strategies.Create(strategyName, params)
	//   worker.SetStrategy(strategy)
	log.Printf("WARNING: Strategy registry not yet implemented in Go. Worker will exit.")
	log.Printf("To use the worker, implement strategies and call worker.SetStrategy().")

	if *restore {
		worker.RestoreState()
	}

	// worker.Run() would be called here once strategies are registered.
	// For now, just save state and exit.
	worker.SaveState()
	log.Printf("Worker exiting -- strategy registry not yet available.")
}

func parseParams(s string) map[string]interface{} {
	params := make(map[string]interface{})
	if s == "" {
		return params
	}
	for _, pair := range strings.Split(s, ",") {
		parts := strings.SplitN(pair, "=", 2)
		if len(parts) != 2 {
			continue
		}
		k := strings.TrimSpace(parts[0])
		v := parts[1]
		if iv, err := strconv.Atoi(v); err == nil {
			params[k] = iv
		} else if fv, err := strconv.ParseFloat(v, 64); err == nil {
			params[k] = fv
		} else {
			params[k] = v
		}
	}
	return params
}
