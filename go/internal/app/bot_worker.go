// Package app - bot_worker.go
// Bot worker process -- runs a single strategy independently.
// Ported from Python app/bot_worker.py.

package app

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// BotWorker is a standalone bot worker that runs one strategy via the tick stream.
type BotWorker struct {
	StrategyName   string
	Params         map[string]interface{}
	SocketPath     string
	Cash           float64
	MarketDir      string
	DryRun         bool
	SessionCap     *float64
	PerSession     bool
	WindowDuration int

	Position     *core.Position
	Executor     core.Executor
	Strategy     core.Strategy
	Running      bool
	StartingCash float64
	SessionCount int

	workersDir     string
	botTradesFile  string
	botSessionLog  string
	positionDir    string
	stateDir       string
	sessionReserve float64

	privateKey  string
	safeAddress string
}

// NewBotWorker creates a new BotWorker.
func NewBotWorker(strategyName string, params map[string]interface{}, socketPath string,
	cash float64, marketDir string, dryRun bool, sessionCap *float64,
	perSession bool, windowDuration int) (*BotWorker, error) {

	if params == nil {
		params = make(map[string]interface{})
	}
	if windowDuration <= 0 {
		windowDuration = 300
	}

	w := &BotWorker{
		StrategyName:   strategyName,
		Params:         params,
		SocketPath:     socketPath,
		Cash:           cash,
		MarketDir:      marketDir,
		DryRun:         dryRun,
		SessionCap:     sessionCap,
		PerSession:     perSession,
		WindowDuration: windowDuration,
		Running:        true,
		Position:       &core.Position{Cash: cash},
		StartingCash:   cash,

		workersDir:    filepath.Join(marketDir, "workers"),
		botTradesFile: filepath.Join(marketDir, "bot_trades.jsonl"),
		botSessionLog: filepath.Join(marketDir, "bot_log.jsonl"),
		positionDir:   filepath.Join(marketDir, "bot_positions"),
	}

	w.stateDir = filepath.Join(w.workersDir, strategyName)
	os.MkdirAll(w.stateDir, 0o755)

	// Set up executor.
	if dryRun {
		w.Executor = core.NewSimExecutor("book", 0)
	} else {
		if err := w.initClobExecutor(); err != nil {
			return nil, err
		}
	}

	// Set up signal handling.
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigChan
		log.Printf("[%s] Shutdown signal received.", w.StrategyName)
		w.Running = false
	}()

	return w, nil
}

// SetStrategy sets the strategy for this worker. Must be called before Run().
func (w *BotWorker) SetStrategy(strategy core.Strategy) {
	w.Strategy = strategy
}

func (w *BotWorker) initClobExecutor() error {
	// TODO: Implement CLOB executor initialization with auth.
	// For now, return an error indicating it needs go-ethereum.
	return fmt.Errorf("CLOB executor not yet implemented - requires go-ethereum. Use --dry-run")
}

// SaveState writes worker state to disk.
func (w *BotWorker) SaveState() {
	bankroll := w.Position.Cash + w.sessionReserve
	state := map[string]interface{}{
		"strategy":      w.StrategyName,
		"params":        w.Params,
		"cash":          math.Round(bankroll*10000) / 10000,
		"dry_run":       w.DryRun,
		"per_session":   w.PerSession,
		"session_count": w.SessionCount,
		"pid":           os.Getpid(),
		"timestamp":     time.Now().UTC().Format(time.RFC3339),
	}
	if w.SessionCap != nil {
		state["session_cap"] = *w.SessionCap
	}

	fp := filepath.Join(w.stateDir, "state.json")
	data, _ := json.MarshalIndent(state, "", "  ")
	tmp := fp + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err == nil {
		os.Rename(tmp, fp)
	}
}

// RestoreState restores cash and session count from last run.
func (w *BotWorker) RestoreState() {
	fp := filepath.Join(w.stateDir, "state.json")
	data, err := os.ReadFile(fp)
	if err != nil {
		return
	}
	var state map[string]interface{}
	if json.Unmarshal(data, &state) != nil {
		return
	}
	if cash, ok := state["cash"].(float64); ok {
		w.Position.Cash = cash
		w.StartingCash = cash
		w.Cash = cash
		if sc, ok := state["session_count"].(float64); ok {
			w.SessionCount = int(sc)
		}
		log.Printf("[%s] Restored state: cash=$%.2f, sessions=%d",
			w.StrategyName, w.Position.Cash, w.SessionCount)
	}
}

// Run is the main loop -- connect to tick stream, process sessions.
func (w *BotWorker) Run() {
	if w.Strategy == nil {
		log.Printf("[%s] ERROR: no strategy set. Call SetStrategy() before Run().", w.StrategyName)
		return
	}

	subscriber := core.NewTickSubscriber(w.SocketPath, 2*time.Second)
	log.Printf("[%s] Bot worker started (PID %d, cash=$%.2f, dry_run=%v, market_dir=%s)",
		w.StrategyName, os.Getpid(), w.Position.Cash, w.DryRun, w.MarketDir)

	var trader *core.Trader
	var metadataDict map[string]interface{}
	var tradeDicts []map[string]interface{}
	tradeCount := 0
	traderInitialized := false

	msgCh := subscriber.Messages(func() bool { return w.Running })
	for msg := range msgCh {
		if !w.Running {
			break
		}

		msgType, _ := msg["type"].(string)

		switch msgType {
		case "session_start":
			metadataDict, _ = msg["metadata"].(map[string]interface{})
			trader = nil
			traderInitialized = false
			tradeDicts = nil
			tradeCount = 0

			if w.PerSession {
				w.Position.Cash = w.Cash
				w.StartingCash = w.Cash
			}

			w.sessionReserve = 0
			if w.SessionCap != nil && w.Position.Cash > *w.SessionCap {
				w.sessionReserve = w.Position.Cash - *w.SessionCap
				w.Position.Cash = *w.SessionCap
				log.Printf("[%s] Session cap $%.2f: deploying $%.2f, reserve $%.2f",
					w.StrategyName, *w.SessionCap, w.Position.Cash, w.sessionReserve)
			}

			w.StartingCash = w.Position.Cash + w.sessionReserve
			w.SaveState()
			log.Printf("[%s] Session starting: cash=$%.2f (reserve=$%.2f)",
				w.StrategyName, w.Position.Cash, w.sessionReserve)

		case "tick":
			if metadataDict == nil {
				continue
			}

			sampleDict, ok := msg["sample"].(map[string]interface{})
			if !ok {
				continue
			}

			if !traderInitialized {
				initialBtcPrice := 0.0
				if v, ok := sampleDict["btc_price_usd"].(float64); ok {
					initialBtcPrice = v
				}

				trader = core.NewTrader(w.Strategy, w.Executor)
				refBalance := w.Position.Cash
				trader.StartSession(metadataDict, w.Position, initialBtcPrice,
					float64(w.WindowDuration), refBalance, true)
				traderInitialized = true
			}

			trade := trader.ProcessTick(sampleDict)

			// Process limit order fills.
			for _, fill := range trader.LastTickFills() {
				td := tradeToDict(&fill)
				tradeDicts = append(tradeDicts, td)
				tradeCount++
				if fill.Success == nil || (fill.Success != nil && *fill.Success) {
					if !w.DryRun {
						savePosition(w.positionDir, w.StrategyName, w.Position)
					}
				}
				log.Printf("[%s] Limit fill: %s @ %.2f x %.2f | order_id=%s",
					w.StrategyName, fill.Side, fill.Price, fill.Tokens, fill.OrderID)
			}

			if trade != nil {
				td := tradeToDict(trade)
				tradeDicts = append(tradeDicts, td)
				if trade.Success == nil || (trade.Success != nil && *trade.Success) {
					tradeCount++
					if trade.Success != nil && *trade.Success && !w.DryRun {
						savePosition(w.positionDir, w.StrategyName, w.Position)
					}
					log.Printf("[%s] Trade executed: %s @ %.2f x %.2f | order_id=%s",
						w.StrategyName, trade.Side, trade.Price, trade.Tokens, trade.OrderID)
				} else {
					log.Printf("[%s] Trade failed: %s", w.StrategyName, trade.Error)
				}
			}

			w.writeLiveTrades(tradeDicts, tradeCount)

		case "session_end":
			if trader != nil {
				cancelled := trader.CancelAllPending()
				if cancelled > 0 {
					log.Printf("[%s] Cancelled %d pending orders at session end",
						w.StrategyName, cancelled)
					if !w.DryRun {
						savePosition(w.positionDir, w.StrategyName, w.Position)
					}
				}

				outcome, _ := msg["outcome"].(string)
				sessionFilepath, _ := msg["session_filepath"].(string)
				w.resolve(outcome, tradeCount, tradeDicts, trader, sessionFilepath)
			}

			trader = nil
			metadataDict = nil
			w.SessionCount++
			w.SaveState()
		}
	}

	subscriber.Close()
	log.Printf("[%s] Bot worker stopped.", w.StrategyName)
}

func (w *BotWorker) resolve(outcome string, tradeCount int, tradeDicts []map[string]interface{},
	trader *core.Trader, sessionFilepath string) {

	credit := 0.0
	switch outcome {
	case "Up":
		credit = w.Position.UpTokens
	case "Down":
		credit = w.Position.DownTokens
	}

	w.Position.UpTokens = 0
	w.Position.DownTokens = 0

	if w.sessionReserve > 0 {
		w.Position.Cash += w.sessionReserve
		log.Printf("[%s] Restored reserve $%.2f -> total cash $%.2f",
			w.StrategyName, w.sessionReserve, w.Position.Cash)
		w.sessionReserve = 0
	}

	if credit > 0 {
		old := w.Position.Cash
		w.Position.Cash += credit
		log.Printf("[%s] Resolution credit: $%.4f -> $%.4f (+$%.4f) [%s]",
			w.StrategyName, old, w.Position.Cash, credit, outcome)
	}

	pnl := w.Position.Cash - w.StartingCash
	log.Printf("[%s] Session resolved: %s | Trades: %d | Cash: $%.2f | PnL: $%.2f",
		w.StrategyName, outcome, tradeCount, w.Position.Cash, pnl)

	// Write session summary to bot_log.jsonl.
	summary := map[string]interface{}{
		"timestamp":      time.Now().UTC().Format(time.RFC3339),
		"strategy":       w.StrategyName,
		"outcome":        outcome,
		"trades":         tradeCount,
		"cash":           math.Round(w.Position.Cash*10000) / 10000,
		"pnl":            math.Round(pnl*10000) / 10000,
		"credit":         math.Round(credit*10000) / 10000,
		"session_number": w.SessionCount,
	}
	if data, err := json.Marshal(summary); err == nil {
		appendJSONLine(w.botSessionLog, data)
	}

	// Write resolution record to bot_trades.jsonl.
	resolution := map[string]interface{}{
		"type":          "resolution",
		"timestamp":     time.Now().UTC().Format(time.RFC3339),
		"strategy":      w.StrategyName,
		"outcome":       outcome,
		"starting_cash": math.Round(w.StartingCash*10000) / 10000,
		"final_cash":    math.Round(w.Position.Cash*10000) / 10000,
		"pnl":           math.Round(pnl*10000) / 10000,
		"credit":        math.Round(credit*10000) / 10000,
		"trades":        tradeCount,
		"up_tokens":     math.Round(w.Position.UpTokens*10000) / 10000,
		"down_tokens":   math.Round(w.Position.DownTokens*10000) / 10000,
		"dry_run":       w.DryRun,
	}
	if data, err := json.Marshal(resolution); err == nil {
		appendJSONLine(w.botTradesFile, data)
	}

	// Write trading sidecar if trades occurred.
	if sessionFilepath != "" && tradeCount > 0 {
		sidecarPath := fmt.Sprintf("%s.trading.%s.json", sessionFilepath, w.StrategyName)
		sidecar := map[string]interface{}{
			"strategy":      w.StrategyName,
			"params":        w.Params,
			"dry_run":       w.DryRun,
			"starting_cash": math.Round(w.StartingCash*10000) / 10000,
			"final_cash":    math.Round(w.Position.Cash*10000) / 10000,
			"pnl":           math.Round(pnl*10000) / 10000,
			"outcome":       outcome,
			"trades":        tradeDicts,
		}
		if decLog := trader.DecisionLog(); len(decLog) > 0 {
			sidecar["decision_log"] = decLog
		}
		if data, err := json.MarshalIndent(sidecar, "", "  "); err == nil {
			if err := os.WriteFile(sidecarPath, data, 0o644); err == nil {
				log.Printf("Trading sidecar saved: %s", sidecarPath)
			}
		}
	}

	// Background claim for live trades.
	if tradeCount > 0 && !w.DryRun && w.privateKey != "" {
		if HasBuilderCreds() {
			ClaimBackground(w.privateKey, w.safeAddress)
		}
	}

	// Remove live trades file.
	livePath := filepath.Join(w.stateDir, "live_trades.json")
	os.Remove(livePath)
}

func (w *BotWorker) writeLiveTrades(tradeDicts []map[string]interface{}, tradeCount int) {
	fp := filepath.Join(w.stateDir, "live_trades.json")
	// Keep only last 50 trades.
	displayTrades := tradeDicts
	if len(displayTrades) > 50 {
		displayTrades = displayTrades[len(displayTrades)-50:]
	}
	data := map[string]interface{}{
		"strategy":    w.StrategyName,
		"dry_run":     w.DryRun,
		"cash":        math.Round(w.Position.Cash*10000) / 10000,
		"trade_count": tradeCount,
		"trades":      displayTrades,
	}
	if jsonData, err := json.Marshal(data); err == nil {
		os.WriteFile(fp, jsonData, 0o644)
	}
}

func tradeToDict(trade *core.Trade) map[string]interface{} {
	d := map[string]interface{}{
		"tick_index": trade.TickIndex,
		"side":       trade.Side,
		"price":      trade.Price,
		"amount":     math.Round(trade.Amount*10000) / 10000,
		"tokens":     math.Round(trade.Tokens*10000) / 10000,
		"order_id":   trade.OrderID,
		"error":      trade.Error,
		"timestamp":  trade.Timestamp,
	}
	if trade.Success != nil {
		d["success"] = *trade.Success
	}
	if trade.ElapsedSeconds != nil {
		d["elapsed_seconds"] = *trade.ElapsedSeconds
	}
	return d
}

func savePosition(positionDir, slotName string, position *core.Position) {
	os.MkdirAll(positionDir, 0o755)
	fp := filepath.Join(positionDir, slotName+".json")
	data, _ := json.Marshal(position.ToDict())
	tmp := fp + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err == nil {
		os.Rename(tmp, fp)
	}
}

func appendJSONLine(filepath string, data []byte) {
	f, err := os.OpenFile(filepath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		log.Printf("Failed to write to %s: %v", filepath, err)
		return
	}
	defer f.Close()
	f.Write(data)
	f.Write([]byte("\n"))
}
