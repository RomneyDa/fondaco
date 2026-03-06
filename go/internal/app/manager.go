// Package app - manager.go
// Central AppManager: manages multiple MarketRunners.
// Ported from Python app/app_manager.py.

package app

import (
	"encoding/json"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// AppManager is the central manager for multiple markets.
// Owns shared resources and delegates per-market work to MarketRunner instances.
type AppManager struct {
	Port    int
	Running bool

	HTTPClient *core.LoggingClient
	RTDS       *core.RTDSClient

	mu      sync.Mutex
	markets map[string]*MarketRunner
	threads map[string]chan struct{} // done channels for each market goroutine
}

// NewAppManager creates a new AppManager.
func NewAppManager(port int) *AppManager {
	if port <= 0 {
		port = core.DefaultControlPort
	}

	httpClient := core.NewLoggingClient(15 * time.Second)
	rtds := core.NewRTDSClient("btc/usd")
	rtds.Start()
	core.SetClient(httpClient)

	return &AppManager{
		Port:       port,
		Running:    true,
		HTTPClient: httpClient,
		RTDS:       rtds,
		markets:    make(map[string]*MarketRunner),
		threads:    make(map[string]chan struct{}),
	}
}

// StartMarket starts data collection for a market. Returns the runner.
func (m *AppManager) StartMarket(slugPrefix string, windowDuration int, marketType string, maxSessions int) (*MarketRunner, error) {
	if windowDuration <= 0 {
		windowDuration = core.WindowDurationSeconds
	}
	if marketType == "" {
		marketType = "updown"
	}

	slugKey := core.SlugKey(slugPrefix)

	m.mu.Lock()
	if _, exists := m.markets[slugKey]; exists {
		m.mu.Unlock()
		return nil, &MarketError{Msg: "Market '" + slugKey + "' is already running."}
	}
	m.mu.Unlock()

	runner := NewMarketRunner(slugPrefix, m.HTTPClient, m.RTDS, marketType, windowDuration, maxSessions)

	done := make(chan struct{})
	go func() {
		defer close(done)
		m.runMarket(slugKey, runner)
	}()

	m.mu.Lock()
	m.markets[slugKey] = runner
	m.threads[slugKey] = done
	m.mu.Unlock()

	log.Printf("Started market: %s (type=%s, window=%ds)", slugKey, marketType, windowDuration)
	m.saveState()
	return runner, nil
}

func (m *AppManager) runMarket(slugKey string, runner *MarketRunner) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("Market %s crashed: %v", slugKey, r)
		}
		m.mu.Lock()
		delete(m.markets, slugKey)
		delete(m.threads, slugKey)
		m.mu.Unlock()
		m.saveState()
		log.Printf("Market %s stopped and cleaned up.", slugKey)
	}()
	runner.Run()
}

// StopMarket stops a running market.
func (m *AppManager) StopMarket(slugPrefix string, afterSession bool) error {
	slugKey := core.SlugKey(slugPrefix)
	m.mu.Lock()
	runner, ok := m.markets[slugKey]
	done := m.threads[slugKey]
	m.mu.Unlock()

	if !ok || runner == nil {
		return &MarketError{Msg: "Market '" + slugKey + "' is not running."}
	}

	if afterSession {
		runner.RequestStopAfterSession()
	} else {
		runner.RequestStop()
	}

	// Wait for goroutine to finish.
	if done != nil {
		select {
		case <-done:
		case <-time.After(30 * time.Second):
			log.Printf("Market %s goroutine did not exit within 30s.", slugKey)
		}
	}
	return nil
}

// GetMarket returns a running market runner.
func (m *AppManager) GetMarket(slugPrefix string) (*MarketRunner, error) {
	slugKey := core.SlugKey(slugPrefix)
	m.mu.Lock()
	runner, ok := m.markets[slugKey]
	m.mu.Unlock()
	if !ok || runner == nil {
		return nil, &MarketError{Msg: "Market '" + slugKey + "' is not running."}
	}
	return runner, nil
}

// GetStatus returns aggregate status from all markets.
func (m *AppManager) GetStatus() map[string]interface{} {
	m.mu.Lock()
	var marketStatuses []interface{}
	for _, runner := range m.markets {
		marketStatuses = append(marketStatuses, runner.GetStatus())
	}
	m.mu.Unlock()

	return map[string]interface{}{
		"running": m.Running,
		"markets": marketStatuses,
	}
}

// RequestStop stops everything -- all markets and their workers.
func (m *AppManager) RequestStop() {
	log.Printf("App stop requested.")
	m.Running = false
	m.mu.Lock()
	runners := make([]*MarketRunner, 0, len(m.markets))
	for _, r := range m.markets {
		runners = append(runners, r)
	}
	m.mu.Unlock()
	for _, r := range runners {
		r.RequestStop()
	}
}

// RequestStopAfterSession stops all markets after their current sessions finish.
func (m *AppManager) RequestStopAfterSession() {
	log.Printf("App kill requested -- stopping all markets after session.")
	m.mu.Lock()
	runners := make([]*MarketRunner, 0, len(m.markets))
	for _, r := range m.markets {
		runners = append(runners, r)
	}
	m.mu.Unlock()
	for _, r := range runners {
		r.RequestStopAfterSession()
	}
}

// Run blocks on the main goroutine until stopped. Handles SIGINT/SIGTERM.
func (m *AppManager) Run() {
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		<-sigChan
		log.Printf("Shutdown signal received.")
		m.RequestStop()
	}()

	for m.Running {
		time.Sleep(1 * time.Second)
		m.mu.Lock()
		hasMarkets := len(m.markets) > 0
		m.mu.Unlock()
		if !hasMarkets && !m.Running {
			break
		}
	}

	m.shutdown()
}

func (m *AppManager) shutdown() {
	m.mu.Lock()
	runners := make([]*MarketRunner, 0, len(m.markets))
	dones := make([]chan struct{}, 0, len(m.threads))
	for _, r := range m.markets {
		runners = append(runners, r)
	}
	for _, d := range m.threads {
		dones = append(dones, d)
	}
	m.mu.Unlock()

	for _, r := range runners {
		r.RequestStop()
	}
	for _, d := range dones {
		select {
		case <-d:
		case <-time.After(30 * time.Second):
		}
	}

	m.RTDS.Stop()
	m.HTTPClient.Close()
	log.Printf("AppManager shutdown complete.")
}

func (m *AppManager) saveState() {
	m.mu.Lock()
	var marketConfigs []map[string]interface{}
	for slugKey, runner := range m.markets {
		marketConfigs = append(marketConfigs, map[string]interface{}{
			"slug_prefix":     runner.SlugPrefix,
			"slug_key":        slugKey,
			"market_type":     runner.MarketType,
			"window_duration": runner.WindowDuration,
		})
	}
	m.mu.Unlock()

	state := map[string]interface{}{
		"pid":     os.Getpid(),
		"port":    m.Port,
		"markets": marketConfigs,
	}

	stateFile := core.AppStateFile()
	os.MkdirAll(filepath.Dir(stateFile), 0o755)

	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		log.Printf("Failed to save app state: %v", err)
		return
	}

	tmp := stateFile + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		log.Printf("Failed to save app state: %v", err)
		return
	}
	os.Rename(tmp, stateFile)
}

// Restore restores markets and strategies from app.json.
func (m *AppManager) Restore() {
	stateFile := core.AppStateFile()
	if _, err := os.Stat(stateFile); os.IsNotExist(err) {
		log.Printf("No app state file -- nothing to restore.")
		return
	}

	data, err := os.ReadFile(stateFile)
	if err != nil {
		log.Printf("Failed to read app state: %v", err)
		return
	}

	var state map[string]interface{}
	if err := json.Unmarshal(data, &state); err != nil {
		log.Printf("Failed to parse app state: %v", err)
		return
	}

	marketsRaw, ok := state["markets"]
	if !ok {
		log.Printf("App state has no markets -- nothing to restore.")
		return
	}

	markets, ok := marketsRaw.([]interface{})
	if !ok || len(markets) == 0 {
		log.Printf("App state has no markets -- nothing to restore.")
		return
	}

	for _, mcRaw := range markets {
		mc, ok := mcRaw.(map[string]interface{})
		if !ok {
			continue
		}
		slugPrefix, _ := mc["slug_prefix"].(string)
		if slugPrefix == "" {
			continue
		}
		windowDuration := core.WindowDurationSeconds
		if wd, ok := mc["window_duration"].(float64); ok {
			windowDuration = int(wd)
		}
		marketType, _ := mc["market_type"].(string)
		if marketType == "" {
			marketType = "updown"
		}

		runner, err := m.StartMarket(slugPrefix, windowDuration, marketType, 0)
		if err != nil {
			log.Printf("Failed to restore market '%s': %v", slugPrefix, err)
			continue
		}
		time.Sleep(500 * time.Millisecond)
		runner.RestoreStrategies()
		log.Printf("Restored market: %s", slugPrefix)
	}
}

// MarketError is a simple error type for market operations.
type MarketError struct {
	Msg string
}

func (e *MarketError) Error() string {
	return e.Msg
}
