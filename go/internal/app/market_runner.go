// Package app - market_runner.go
// MarketRunner: data collection + worker management for a single market.
// Ported from Python app/market_runner.py.

package app

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// WorkerInfo tracks a spawned bot worker process.
type WorkerInfo struct {
	StrategyName string                 `json:"strategy_name"`
	Params       map[string]interface{} `json:"params"`
	Cash         float64                `json:"cash"`
	DryRun       bool                   `json:"dry_run"`
	PerSession   bool                   `json:"per_session"`
	SessionCap   *float64               `json:"session_cap"`
	Process      *exec.Cmd              `json:"-"`
	StartedAt    string                 `json:"started_at"`
}

// MarketRunner handles data collection + worker process management for one market.
type MarketRunner struct {
	SlugPrefix     string
	SlugKey        string
	MarketType     string
	WindowDuration int
	PollInterval   float64
	MaxSessions    int

	fetcher     *core.PriceFetcher
	recorder    *core.SessionRecorder
	broadcaster *core.TickBroadcaster

	Running          bool
	stopAfterSession bool
	SessionCount     int

	mu      sync.Mutex
	workers []*WorkerInfo

	marketDir      string
	workersDir     string
	strategiesFile string
	socketPath     string
}

// NewMarketRunner creates a new MarketRunner.
func NewMarketRunner(slugPrefix string, httpClient *core.LoggingClient, rtds *core.RTDSClient,
	marketType string, windowDuration int, maxSessions int) *MarketRunner {

	slugKey := core.SlugKey(slugPrefix)
	marketDir := core.MarketDataDir(slugKey)
	socketPath := core.MarketSocketPath(slugKey)

	os.MkdirAll(filepath.Dir(socketPath), 0o755)

	return &MarketRunner{
		SlugPrefix:     slugPrefix,
		SlugKey:        slugKey,
		MarketType:     marketType,
		WindowDuration: windowDuration,
		PollInterval:   core.PollIntervalSeconds,
		MaxSessions:    maxSessions,
		fetcher:        core.NewPriceFetcher(httpClient, rtds),
		recorder:       core.NewSessionRecorder(slugPrefix, "", "", ""),
		broadcaster:    core.NewTickBroadcaster(socketPath),
		Running:        true,
		marketDir:      marketDir,
		workersDir:     core.MarketWorkersDir(slugKey),
		strategiesFile: core.MarketStrategiesFile(slugKey),
		socketPath:     socketPath,
	}
}

// RequestStop signals the runner to stop immediately.
func (r *MarketRunner) RequestStop() {
	log.Printf("[%s] Stop requested.", r.SlugKey)
	r.Running = false
}

// RequestStopAfterSession signals the runner to stop after the current session.
func (r *MarketRunner) RequestStopAfterSession() {
	log.Printf("[%s] Kill requested -- will stop after current session.", r.SlugKey)
	r.stopAfterSession = true
}

// EnableTrading spawns a bot worker process for the given strategy.
func (r *MarketRunner) EnableTrading(strategyName string, params map[string]interface{},
	cash *float64, dryRun bool, perSession bool, sessionCap *float64) error {

	if params == nil {
		params = make(map[string]interface{})
	}

	// Determine cash.
	startingCash := 100.0
	if cash != nil {
		startingCash = *cash
	} else if !dryRun {
		stateFile := filepath.Join(r.workersDir, strategyName, "state.json")
		if data, err := os.ReadFile(stateFile); err == nil {
			var saved map[string]interface{}
			if json.Unmarshal(data, &saved) == nil {
				if c, ok := saved["cash"].(float64); ok {
					startingCash = c
					log.Printf("[%s/%s] Using saved cash from last run: $%.2f",
						r.SlugKey, strategyName, startingCash)
				}
			}
		}
	}

	r.mu.Lock()
	r.killWorkerByName(strategyName)
	process, err := r.spawnWorker(strategyName, params, startingCash, dryRun, perSession, sessionCap)
	if err != nil {
		r.mu.Unlock()
		return fmt.Errorf("failed to spawn worker: %v", err)
	}

	worker := &WorkerInfo{
		StrategyName: strategyName,
		Params:       params,
		Cash:         startingCash,
		DryRun:       dryRun,
		PerSession:   perSession,
		SessionCap:   sessionCap,
		Process:      process,
		StartedAt:    time.Now().UTC().Format(time.RFC3339),
	}
	r.workers = append(r.workers, worker)
	r.mu.Unlock()

	pid := 0
	if process != nil && process.Process != nil {
		pid = process.Process.Pid
	}
	log.Printf("[%s] Worker spawned: strategy=%s params=%v cash=$%.2f dry_run=%v per_session=%v session_cap=%v pid=%d",
		r.SlugKey, strategyName, params, startingCash, dryRun, perSession, sessionCap, pid)

	r.saveStrategiesConfig()
	return nil
}

func (r *MarketRunner) spawnWorker(strategyName string, params map[string]interface{},
	cash float64, dryRun bool, perSession bool, sessionCap *float64) (*exec.Cmd, error) {

	// Build command to run the worker binary.
	workerBin := filepath.Join(core.BaseDir, "go", "bin", "worker")
	args := []string{
		"--strategy", strategyName,
		"--socket", r.socketPath,
		"--market-dir", r.marketDir,
		"--cash", fmt.Sprintf("%.4f", cash),
		"--window", fmt.Sprintf("%d", r.WindowDuration),
		"--restore",
	}

	if len(params) > 0 {
		paramStr := ""
		for k, v := range params {
			if paramStr != "" {
				paramStr += ","
			}
			paramStr += fmt.Sprintf("%s=%v", k, v)
		}
		args = append(args, "--params", paramStr)
	}
	if dryRun {
		args = append(args, "--dry-run")
	}
	if perSession {
		args = append(args, "--per-session")
	}
	if sessionCap != nil {
		args = append(args, "--session-cap", fmt.Sprintf("%.4f", *sessionCap))
	}

	logDir := filepath.Join(r.workersDir, strategyName)
	os.MkdirAll(logDir, 0o755)
	logFilePath := filepath.Join(logDir, "worker.log")
	logFH, err := os.OpenFile(logFilePath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}

	cmd := exec.Command(workerBin, args...)
	cmd.Stdout = logFH
	cmd.Stderr = logFH
	cmd.Dir = core.BaseDir
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}

	if err := cmd.Start(); err != nil {
		logFH.Close()
		return nil, err
	}

	log.Printf("[%s] Spawned worker %s (PID %d), logging to %s",
		r.SlugKey, strategyName, cmd.Process.Pid, logFilePath)
	return cmd, nil
}

// killWorkerByName kills a worker process by strategy name. Caller must hold lock.
func (r *MarketRunner) killWorkerByName(strategyName string) {
	var remaining []*WorkerInfo
	for _, w := range r.workers {
		if w.StrategyName == strategyName {
			if w.Process != nil && w.Process.Process != nil {
				log.Printf("[%s] Killing worker %s (PID %d)", r.SlugKey, w.StrategyName, w.Process.Process.Pid)
				w.Process.Process.Signal(syscall.SIGTERM)
				done := make(chan error, 1)
				go func(p *exec.Cmd) { done <- p.Wait() }(w.Process)
				select {
				case <-done:
				case <-time.After(10 * time.Second):
					w.Process.Process.Kill()
				}
			}
		} else {
			remaining = append(remaining, w)
		}
	}
	r.workers = remaining
}

func (r *MarketRunner) killAllWorkers() {
	r.mu.Lock()
	defer r.mu.Unlock()

	for _, w := range r.workers {
		if w.Process != nil && w.Process.Process != nil {
			log.Printf("[%s] Killing worker %s (PID %d)", r.SlugKey, w.StrategyName, w.Process.Process.Pid)
			w.Process.Process.Signal(syscall.SIGTERM)
		}
	}
	for _, w := range r.workers {
		if w.Process != nil {
			done := make(chan error, 1)
			go func(p *exec.Cmd) { done <- p.Wait() }(w.Process)
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				if w.Process.Process != nil {
					w.Process.Process.Kill()
				}
			}
		}
	}
	r.workers = nil
}

// DisableTrading kills a worker (or all workers if strategyName is empty).
func (r *MarketRunner) DisableTrading(strategyName string) error {
	r.mu.Lock()
	if strategyName != "" {
		found := false
		for _, w := range r.workers {
			if w.StrategyName == strategyName {
				found = true
				break
			}
		}
		if !found {
			r.mu.Unlock()
			return fmt.Errorf("no active worker for strategy '%s'", strategyName)
		}
		r.killWorkerByName(strategyName)
		log.Printf("[%s] Worker killed: %s", r.SlugKey, strategyName)
	} else {
		for _, w := range r.workers {
			if w.Process != nil && w.Process.Process != nil {
				w.Process.Process.Signal(syscall.SIGTERM)
			}
		}
		for _, w := range r.workers {
			if w.Process != nil {
				done := make(chan error, 1)
				go func(p *exec.Cmd) { done <- p.Wait() }(w.Process)
				select {
				case <-done:
				case <-time.After(10 * time.Second):
					if w.Process.Process != nil {
						w.Process.Process.Kill()
					}
				}
			}
		}
		r.workers = nil
		log.Printf("[%s] All workers killed.", r.SlugKey)
	}
	r.mu.Unlock()
	r.saveStrategiesConfig()
	return nil
}

// SetCash updates the cash for a worker.
func (r *MarketRunner) SetCash(cash float64, strategyName string) error {
	r.mu.Lock()
	if len(r.workers) == 0 {
		r.mu.Unlock()
		return fmt.Errorf("no active workers to update")
	}
	var targets []*WorkerInfo
	if strategyName != "" {
		for _, w := range r.workers {
			if w.StrategyName == strategyName {
				targets = append(targets, w)
			}
		}
		if len(targets) == 0 {
			r.mu.Unlock()
			return fmt.Errorf("no active worker for strategy '%s'", strategyName)
		}
	} else if len(r.workers) == 1 {
		targets = []*WorkerInfo{r.workers[0]}
	} else {
		names := make([]string, 0, len(r.workers))
		for _, w := range r.workers {
			names = append(names, w.StrategyName)
		}
		r.mu.Unlock()
		return fmt.Errorf("multiple workers active -- specify which one: %v", names)
	}
	r.mu.Unlock()

	for _, w := range targets {
		stateFile := filepath.Join(r.workersDir, w.StrategyName, "state.json")
		var state map[string]interface{}
		if data, err := os.ReadFile(stateFile); err == nil {
			json.Unmarshal(data, &state)
		}
		if state == nil {
			state = make(map[string]interface{})
		}
		state["cash"] = math.Round(cash*10000) / 10000

		data, _ := json.MarshalIndent(state, "", "  ")
		tmp := stateFile + ".tmp"
		if err := os.WriteFile(tmp, data, 0o644); err == nil {
			os.Rename(tmp, stateFile)
			log.Printf("[%s/%s] Cash updated in state file: $%.4f", r.SlugKey, w.StrategyName, cash)
		} else {
			log.Printf("Failed to update cash for %s: %v", w.StrategyName, err)
		}
	}
	return nil
}

func (r *MarketRunner) saveStrategiesConfig() {
	r.mu.Lock()
	var configs []map[string]interface{}
	for _, w := range r.workers {
		state := r.readWorkerState(w.StrategyName)
		workerCash := w.Cash
		if state != nil {
			if c, ok := state["cash"].(float64); ok {
				workerCash = c
			}
		}
		entry := map[string]interface{}{
			"name":        w.StrategyName,
			"params":      w.Params,
			"cash":        workerCash,
			"dry_run":     w.DryRun,
			"per_session": w.PerSession,
		}
		if w.SessionCap != nil {
			entry["session_cap"] = *w.SessionCap
		}
		configs = append(configs, entry)
	}
	r.mu.Unlock()

	os.MkdirAll(filepath.Dir(r.strategiesFile), 0o755)
	data, _ := json.MarshalIndent(configs, "", "  ")
	tmp := r.strategiesFile + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err == nil {
		os.Rename(tmp, r.strategiesFile)
	} else {
		log.Printf("Failed to save strategies config: %v", err)
	}
}

// RestoreStrategies restores strategies from the strategies config file.
func (r *MarketRunner) RestoreStrategies() {
	if _, err := os.Stat(r.strategiesFile); os.IsNotExist(err) {
		log.Printf("[%s] No strategies config file found -- nothing to restore.", r.SlugKey)
		return
	}

	data, err := os.ReadFile(r.strategiesFile)
	if err != nil {
		log.Printf("[%s] Failed to read strategies config: %v", r.SlugKey, err)
		return
	}

	var configs []map[string]interface{}
	if err := json.Unmarshal(data, &configs); err != nil {
		log.Printf("[%s] Failed to parse strategies config: %v", r.SlugKey, err)
		return
	}

	if len(configs) == 0 {
		log.Printf("[%s] Strategies config is empty -- nothing to restore.", r.SlugKey)
		return
	}

	for _, entry := range configs {
		name, _ := entry["name"].(string)
		if name == "" {
			continue
		}
		params, _ := entry["params"].(map[string]interface{})
		var cash *float64
		if c, ok := entry["cash"].(float64); ok {
			cash = &c
		}
		dryRun, _ := entry["dry_run"].(bool)
		perSession, _ := entry["per_session"].(bool)
		var sessionCap *float64
		if sc, ok := entry["session_cap"].(float64); ok {
			sessionCap = &sc
		}

		if err := r.EnableTrading(name, params, cash, dryRun, perSession, sessionCap); err != nil {
			log.Printf("[%s] Failed to restore strategy '%s': %v", r.SlugKey, name, err)
		} else {
			log.Printf("[%s] Restored strategy: %s", r.SlugKey, name)
		}
	}
}

// GetStatus returns the runner's status.
func (r *MarketRunner) GetStatus() map[string]interface{} {
	r.mu.Lock()
	var strategies []map[string]interface{}
	for _, w := range r.workers {
		alive := w.Process != nil && w.Process.ProcessState == nil
		if w.Process != nil && w.Process.Process != nil {
			// Check if still running.
			if err := w.Process.Process.Signal(syscall.Signal(0)); err != nil {
				alive = false
			}
		}
		state := r.readWorkerState(w.StrategyName)
		workerCash := w.Cash
		if state != nil {
			if c, ok := state["cash"].(float64); ok {
				workerCash = c
			}
		}
		info := map[string]interface{}{
			"name":          w.StrategyName,
			"params":        w.Params,
			"dry_run":       w.DryRun,
			"cash":          math.Round(workerCash*10000) / 10000,
			"bankroll":      math.Round(workerCash*10000) / 10000,
			"starting_cash": math.Round(w.Cash*10000) / 10000,
			"alive":         alive,
		}
		if w.Process != nil && w.Process.Process != nil {
			info["pid"] = w.Process.Process.Pid
		}
		if w.SessionCap != nil {
			info["session_cap"] = math.Round(*w.SessionCap*10000) / 10000
		}
		strategies = append(strategies, info)
	}
	r.mu.Unlock()

	return map[string]interface{}{
		"slug_prefix":        r.SlugPrefix,
		"slug_key":           r.SlugKey,
		"market_type":        r.MarketType,
		"running":            r.Running,
		"stop_after_session": r.stopAfterSession,
		"session_count":      r.SessionCount,
		"trading_enabled":    len(strategies) > 0,
		"strategies":         strategies,
		"tick_clients":       r.broadcaster.ClientCount(),
	}
}

func (r *MarketRunner) readWorkerState(strategyName string) map[string]interface{} {
	stateFile := filepath.Join(r.workersDir, strategyName, "state.json")
	data, err := os.ReadFile(stateFile)
	if err != nil {
		return nil
	}
	var state map[string]interface{}
	if json.Unmarshal(data, &state) != nil {
		return nil
	}
	return state
}

func (r *MarketRunner) readWorkerLiveTrades(strategyName string) map[string]interface{} {
	tradesFile := filepath.Join(r.workersDir, strategyName, "live_trades.json")
	data, err := os.ReadFile(tradesFile)
	if err != nil {
		return nil
	}
	var trades map[string]interface{}
	if json.Unmarshal(data, &trades) != nil {
		return nil
	}
	return trades
}

// Run is the main loop -- data collection + tick broadcasting.
func (r *MarketRunner) Run() {
	if err := r.broadcaster.Start(); err != nil {
		log.Printf("[%s] Failed to start broadcaster: %v", r.SlugKey, err)
		return
	}

	completed := r.recorder.CountCompletedSessions()
	log.Printf("[%s] MarketRunner started. %d sessions already collected.", r.SlugKey, completed)
	r.recorder.AppendLog("MarketRunner started", map[string]interface{}{
		"slug_prefix":       r.SlugPrefix,
		"existing_sessions": completed,
	})

	defer func() {
		r.killAllWorkers()
		r.broadcaster.Stop()
		log.Printf("[%s] MarketRunner stopped. %d sessions completed.", r.SlugKey, r.SessionCount)
	}()

	for r.Running {
		if err := r.runOneSession(); err != nil {
			if !r.Running {
				break
			}
			log.Printf("[%s] Session error: %v", r.SlugKey, err)
			r.recorder.AppendLog("Session error", map[string]interface{}{"error": err.Error()})
			time.Sleep(10 * time.Second)
			continue
		}

		r.SessionCount++
		log.Printf("[%s] Sessions completed: %d", r.SlugKey, r.SessionCount)

		if r.stopAfterSession {
			log.Printf("[%s] Kill signal -- stopping after session %d.", r.SlugKey, r.SessionCount)
			break
		}
		if r.MaxSessions > 0 && r.SessionCount >= r.MaxSessions {
			log.Printf("[%s] Reached max sessions (%d). Stopping.", r.SlugKey, r.MaxSessions)
			break
		}
	}
}

func (r *MarketRunner) runOneSession() error {
	r.reapDeadWorkers()

	lastStatusWrite := time.Time{}
	onWaiting := func(nextDT time.Time) {
		if time.Since(lastStatusWrite) < 5*time.Second {
			return
		}
		var tradingInfo map[string]interface{}
		r.mu.Lock()
		if len(r.workers) > 0 {
			first := r.workers[0]
			state := r.readWorkerState(first.StrategyName)
			firstCash := first.Cash
			if state != nil {
				if c, ok := state["cash"].(float64); ok {
					firstCash = c
				}
			}
			var strats []map[string]interface{}
			for _, w := range r.workers {
				wState := r.readWorkerState(w.StrategyName)
				wCash := w.Cash
				if wState != nil {
					if c, ok := wState["cash"].(float64); ok {
						wCash = c
					}
				}
				strats = append(strats, map[string]interface{}{
					"name": w.StrategyName,
					"cash": math.Round(wCash*10000) / 10000,
				})
			}
			tradingInfo = map[string]interface{}{
				"enabled":    true,
				"strategy":   first.StrategyName,
				"cash":       math.Round(firstCash*10000) / 10000,
				"strategies": strats,
			}
		}
		r.mu.Unlock()

		r.recorder.WriteLiveStatus("waiting", nextDT.Format(time.RFC3339), tradingInfo)
		r.broadcaster.Broadcast(map[string]interface{}{
			"type":            "waiting",
			"next_window_utc": nextDT.Format(time.RFC3339),
		})
		lastStatusWrite = time.Now()
	}

	windowTS := core.WaitForNextWindow(r.WindowDuration, func() bool { return r.Running }, onWaiting)
	if !r.Running {
		return nil
	}

	metadata := core.FetchMetadataWithRetry(r.SlugPrefix, windowTS, 5)
	if metadata == nil {
		log.Printf("[%s] Could not find market for window %d. Skipping.", r.SlugKey, windowTS)
		return nil
	}

	if r.recorder.SessionExists(metadata.WindowStartUTC) {
		log.Printf("[%s] Session %s already exists. Skipping.", r.SlugKey, metadata.WindowStartUTC)
		return nil
	}

	log.Printf("[%s] COLLECTING: %s", r.SlugKey, metadata.EventTitle)

	session := &core.SessionData{
		Metadata:           *metadata,
		CollectionStartedUTC: time.Now().UTC().Format(time.RFC3339),
	}

	// Broadcast session_start.
	metadataMap := metadataToMap(metadata)
	r.broadcaster.Broadcast(map[string]interface{}{
		"type":           "session_start",
		"metadata":       metadataMap,
		"window_ts":      windowTS,
		"window_end_ts":  windowTS + r.WindowDuration,
	})

	// Poll and broadcast.
	r.pollAndBroadcast(session, metadata, windowTS)

	if len(session.Samples) > 0 {
		session.FinalBtcPriceUSD = session.Samples[len(session.Samples)-1].BtcPriceUSD
	}
	session.CollectionEndedUTC = time.Now().UTC().Format(time.RFC3339)

	if session.InitialBtcPriceUSD != nil && session.FinalBtcPriceUSD != nil {
		if *session.FinalBtcPriceUSD > *session.InitialBtcPriceUSD {
			session.Outcome = core.StringPtr("Up")
			session.OutcomePrices = []string{"1", "0"}
		} else {
			session.Outcome = core.StringPtr("Down")
			session.OutcomePrices = []string{"0", "1"}
		}
	}

	log.Printf("[%s] Window ended. %d samples. BTC: %s -> %s.",
		r.SlugKey, len(session.Samples),
		core.FormatFloat64(session.InitialBtcPriceUSD),
		core.FormatFloat64(session.FinalBtcPriceUSD))

	r.recorder.ClearLiveSession()
	fp := r.recorder.SaveSession(session)
	outcomeStr := ""
	if session.Outcome != nil {
		outcomeStr = *session.Outcome
	}
	log.Printf("[%s] Saved: %s | Outcome: %s | Samples: %d | Errors: %d",
		r.SlugKey, fp, outcomeStr, len(session.Samples), session.ErrorCount)
	r.recorder.AppendLog("Session saved", map[string]interface{}{
		"filepath": fp,
		"outcome":  outcomeStr,
		"samples":  len(session.Samples),
		"errors":   session.ErrorCount,
	})

	r.broadcaster.Broadcast(map[string]interface{}{
		"type":             "session_end",
		"outcome":          outcomeStr,
		"session_filepath": fp,
		"window_ts":        windowTS,
	})

	r.mu.Lock()
	hasWorkers := len(r.workers) > 0
	r.mu.Unlock()
	if hasWorkers {
		r.saveStrategiesConfig()
	}

	return nil
}

func (r *MarketRunner) pollAndBroadcast(session *core.SessionData, metadata *core.MarketMetadata, windowTS int) {
	windowEndTS := float64(windowTS + r.WindowDuration)
	nextSampleTime := float64(time.Now().Unix()) + float64(time.Now().Nanosecond())/1e9
	lastLiveWrite := time.Time{}
	tickIndex := 0

	type sampleResult struct {
		sample core.PriceSample
		err    error
	}
	pending := make(chan sampleResult, 100)
	pendingCount := 0

	for r.Running {
		now := float64(time.Now().Unix()) + float64(time.Now().Nanosecond())/1e9

		if now >= nextSampleTime && nextSampleTime < windowEndTS {
			go func() {
				s := r.fetcher.TakeSample(metadata.UpTokenID, metadata.DownTokenID, metadata.WindowStartUTC)
				pending <- sampleResult{sample: s}
			}()
			pendingCount++
			nextSampleTime += r.PollInterval
		}

		// Drain completed samples.
		draining := true
		for draining {
			select {
			case result := <-pending:
				pendingCount--
				if result.err != nil {
					session.ErrorCount++
					session.Errors = append(session.Errors,
						fmt.Sprintf("%s: %v", time.Now().UTC().Format(time.RFC3339), result.err))
					log.Printf("[%s] Sample error: %v", r.SlugKey, result.err)
				} else {
					sample := result.sample
					if session.InitialBtcPriceUSD == nil {
						session.InitialBtcPriceUSD = sample.BtcPriceUSD
					}
					session.Samples = append(session.Samples, sample)

					sampleMap := sampleToMap(sample)
					r.broadcaster.Broadcast(map[string]interface{}{
						"type":       "tick",
						"sample":     sampleMap,
						"tick_index": tickIndex,
					})
					tickIndex++
				}
			default:
				draining = false
			}
		}

		if time.Since(lastLiveWrite) >= 2*time.Second && len(session.Samples) > 0 {
			tradingInfo := r.aggregateWorkerTradingInfo()
			r.recorder.WriteLiveSession(session, tradingInfo)
			lastLiveWrite = time.Now()
		}

		if now >= windowEndTS && pendingCount <= 0 {
			break
		}

		sleepDur := nextSampleTime - (float64(time.Now().Unix()) + float64(time.Now().Nanosecond())/1e9)
		if sleepDur > 0.01 {
			sleepDur = 0.01
		}
		if sleepDur > 0 {
			time.Sleep(time.Duration(sleepDur * float64(time.Second)))
		}
	}
}

func (r *MarketRunner) aggregateWorkerTradingInfo() map[string]interface{} {
	r.mu.Lock()
	if len(r.workers) == 0 {
		r.mu.Unlock()
		return nil
	}
	workers := make([]*WorkerInfo, len(r.workers))
	copy(workers, r.workers)
	r.mu.Unlock()

	var strategiesInfo []map[string]interface{}
	for _, w := range workers {
		live := r.readWorkerLiveTrades(w.StrategyName)
		if live != nil {
			workerCash := w.Cash
			if c, ok := live["cash"].(float64); ok {
				workerCash = c
			}
			var trades interface{}
			if t, ok := live["trades"]; ok {
				trades = t
			} else {
				trades = []interface{}{}
			}
			strategiesInfo = append(strategiesInfo, map[string]interface{}{
				"name":    w.StrategyName,
				"dry_run": w.DryRun,
				"cash":    workerCash,
				"trades":  trades,
			})
		} else {
			strategiesInfo = append(strategiesInfo, map[string]interface{}{
				"name":    w.StrategyName,
				"dry_run": w.DryRun,
				"cash":    w.Cash,
				"trades":  []interface{}{},
			})
		}
	}

	first := strategiesInfo[0]
	return map[string]interface{}{
		"enabled":    true,
		"strategy":   first["name"],
		"dry_run":    first["dry_run"],
		"cash":       first["cash"],
		"trades":     first["trades"],
		"strategies": strategiesInfo,
	}
}

func (r *MarketRunner) reapDeadWorkers() {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, w := range r.workers {
		if w.Process != nil && w.Process.Process != nil {
			if err := w.Process.Process.Signal(syscall.Signal(0)); err != nil {
				log.Printf("[%s] Worker %s exited", r.SlugKey, w.StrategyName)
			}
		}
	}
}

// SocketPath returns the Unix socket path for this market.
func (r *MarketRunner) SocketPath() string {
	return r.socketPath
}

// Helper to convert metadata to a map for broadcasting.
func metadataToMap(m *core.MarketMetadata) map[string]interface{} {
	return map[string]interface{}{
		"event_id":          m.EventID,
		"event_slug":        m.EventSlug,
		"event_title":       m.EventTitle,
		"condition_id":      m.ConditionID,
		"up_token_id":       m.UpTokenID,
		"down_token_id":     m.DownTokenID,
		"window_start_utc":  m.WindowStartUTC,
		"window_end_utc":    m.WindowEndUTC,
		"resolution_source": m.ResolutionSource,
	}
}

// Helper to convert a PriceSample to a map for broadcasting.
func sampleToMap(s core.PriceSample) map[string]interface{} {
	m := map[string]interface{}{
		"timestamp_utc":  s.TimestampUTC,
		"elapsed_seconds": s.ElapsedSeconds,
	}
	if s.BtcPriceUSD != nil {
		m["btc_price_usd"] = *s.BtcPriceUSD
	}
	if s.UpTokenMidpoint != nil {
		m["up_token_midpoint"] = *s.UpTokenMidpoint
	}
	if s.DownTokenMidpoint != nil {
		m["down_token_midpoint"] = *s.DownTokenMidpoint
	}
	if s.UpTokenBestBid != nil {
		m["up_token_best_bid"] = *s.UpTokenBestBid
	}
	if s.UpTokenBestAsk != nil {
		m["up_token_best_ask"] = *s.UpTokenBestAsk
	}
	if s.DownTokenBestBid != nil {
		m["down_token_best_bid"] = *s.DownTokenBestBid
	}
	if s.DownTokenBestAsk != nil {
		m["down_token_best_ask"] = *s.DownTokenBestAsk
	}
	return m
}
