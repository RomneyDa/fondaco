// Package app - server.go
// HTTP control server using net/http.
// Ported from Python app/server.py.

package app

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"
)

// ControlServer is an HTTP control server wrapping net/http.Server.
type ControlServer struct {
	port    int
	manager *AppManager
	server  *http.Server
}

// NewControlServer creates a new ControlServer.
func NewControlServer(manager *AppManager, port int) *ControlServer {
	cs := &ControlServer{
		port:    port,
		manager: manager,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/api/control/status", cs.corsWrap(cs.handleStatus))
	mux.HandleFunc("/api/control/stop", cs.corsWrap(cs.handleStop))
	mux.HandleFunc("/api/control/kill", cs.corsWrap(cs.handleKill))
	mux.HandleFunc("/api/control/market-on", cs.corsWrap(cs.handleMarketOn))
	mux.HandleFunc("/api/control/market-off", cs.corsWrap(cs.handleMarketOff))
	mux.HandleFunc("/api/control/trade-on", cs.corsWrap(cs.handleTradeOn))
	mux.HandleFunc("/api/control/trade-off", cs.corsWrap(cs.handleTradeOff))
	mux.HandleFunc("/api/control/set-cash", cs.corsWrap(cs.handleSetCash))
	mux.HandleFunc("/api/control/reload", cs.corsWrap(cs.handleReload))

	cs.server = &http.Server{
		Addr:    fmt.Sprintf("0.0.0.0:%d", port),
		Handler: mux,
	}

	return cs
}

// Start begins serving in a goroutine.
func (cs *ControlServer) Start() {
	go func() {
		log.Printf("Control server listening on port %d", cs.port)
		if err := cs.server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("Control server error: %v", err)
		}
	}()
}

// Stop gracefully shuts down the server.
func (cs *ControlServer) Stop() {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cs.server.Shutdown(ctx)
	log.Printf("Control server stopped.")
}

func (cs *ControlServer) corsWrap(handler http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")

		if r.Method == "OPTIONS" {
			w.WriteHeader(204)
			return
		}
		handler(w, r)
	}
}

func sendJSON(w http.ResponseWriter, data interface{}, status int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(data)
}

func readJSONBody(r *http.Request) (map[string]interface{}, error) {
	if r.Body == nil || r.ContentLength == 0 {
		return map[string]interface{}{}, nil
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, err
	}
	if len(body) == 0 {
		return map[string]interface{}{}, nil
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func requireSlug(body map[string]interface{}, w http.ResponseWriter) (string, bool) {
	slug, ok := body["slug_prefix"].(string)
	if !ok || slug == "" {
		sendJSON(w, map[string]interface{}{"error": "Missing 'slug_prefix'"}, 400)
		return "", false
	}
	return slug, true
}

// --- App-level endpoints ---

func (cs *ControlServer) handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != "GET" {
		sendJSON(w, map[string]interface{}{"error": "Method not allowed"}, 405)
		return
	}
	sendJSON(w, cs.manager.GetStatus(), 200)
}

func (cs *ControlServer) handleStop(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sendJSON(w, map[string]interface{}{"error": "Method not allowed"}, 405)
		return
	}
	cs.manager.RequestStop()
	sendJSON(w, map[string]interface{}{"ok": true, "stopping": true}, 200)
}

func (cs *ControlServer) handleKill(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sendJSON(w, map[string]interface{}{"error": "Method not allowed"}, 405)
		return
	}
	cs.manager.RequestStopAfterSession()
	sendJSON(w, map[string]interface{}{"ok": true, "stop_after_session": true}, 200)
}

// --- Market lifecycle ---

func (cs *ControlServer) handleMarketOn(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sendJSON(w, map[string]interface{}{"error": "Method not allowed"}, 405)
		return
	}
	body, err := readJSONBody(r)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	slugPrefix, ok := requireSlug(body, w)
	if !ok {
		return
	}

	windowDuration := 0
	if wd, ok := body["window"].(float64); ok {
		windowDuration = int(wd)
	}
	marketType, _ := body["market_type"].(string)
	if marketType == "" {
		marketType = "updown"
	}
	restore, _ := body["restore"].(bool)

	runner, err := cs.manager.StartMarket(slugPrefix, windowDuration, marketType, 0)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}

	if restore {
		runner.RestoreStrategies()
	}

	sendJSON(w, map[string]interface{}{
		"ok":          true,
		"slug_prefix": slugPrefix,
		"slug_key":    runner.SlugKey,
	}, 200)
}

func (cs *ControlServer) handleMarketOff(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sendJSON(w, map[string]interface{}{"error": "Method not allowed"}, 405)
		return
	}
	body, err := readJSONBody(r)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	slugPrefix, ok := requireSlug(body, w)
	if !ok {
		return
	}
	afterSession, _ := body["after_session"].(bool)

	if err := cs.manager.StopMarket(slugPrefix, afterSession); err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	sendJSON(w, map[string]interface{}{"ok": true, "slug_prefix": slugPrefix}, 200)
}

// --- Bot lifecycle ---

func (cs *ControlServer) handleTradeOn(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sendJSON(w, map[string]interface{}{"error": "Method not allowed"}, 405)
		return
	}
	body, err := readJSONBody(r)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	slugPrefix, ok := requireSlug(body, w)
	if !ok {
		return
	}
	strategyName, _ := body["strategy"].(string)
	if strategyName == "" {
		sendJSON(w, map[string]interface{}{"error": "Missing 'strategy' field"}, 400)
		return
	}
	params, _ := body["params"].(map[string]interface{})
	var cash *float64
	if c, ok := body["cash"].(float64); ok {
		cash = &c
	}
	dryRun, _ := body["dry_run"].(bool)
	perSession, _ := body["per_session"].(bool)
	var sessionCap *float64
	if sc, ok := body["session_cap"].(float64); ok {
		sessionCap = &sc
	}

	runner, err := cs.manager.GetMarket(slugPrefix)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	if err := runner.EnableTrading(strategyName, params, cash, dryRun, perSession, sessionCap); err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	sendJSON(w, map[string]interface{}{
		"ok":          true,
		"trading":     true,
		"strategy":    strategyName,
		"slug_prefix": slugPrefix,
	}, 200)
}

func (cs *ControlServer) handleTradeOff(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sendJSON(w, map[string]interface{}{"error": "Method not allowed"}, 405)
		return
	}
	body, err := readJSONBody(r)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	slugPrefix, ok := requireSlug(body, w)
	if !ok {
		return
	}
	strategyName, _ := body["strategy"].(string)

	runner, err := cs.manager.GetMarket(slugPrefix)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	if err := runner.DisableTrading(strategyName); err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	sendJSON(w, map[string]interface{}{
		"ok":          true,
		"trading":     false,
		"strategy":    strategyName,
		"slug_prefix": slugPrefix,
	}, 200)
}

func (cs *ControlServer) handleSetCash(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sendJSON(w, map[string]interface{}{"error": "Method not allowed"}, 405)
		return
	}
	body, err := readJSONBody(r)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	slugPrefix, ok := requireSlug(body, w)
	if !ok {
		return
	}
	cashRaw, ok := body["cash"]
	if !ok {
		sendJSON(w, map[string]interface{}{"error": "Missing 'cash' field"}, 400)
		return
	}
	cash, ok := cashRaw.(float64)
	if !ok {
		sendJSON(w, map[string]interface{}{"error": "'cash' must be a number"}, 400)
		return
	}
	strategyName, _ := body["strategy"].(string)

	runner, err := cs.manager.GetMarket(slugPrefix)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	if err := runner.SetCash(cash, strategyName); err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	sendJSON(w, map[string]interface{}{
		"ok":          true,
		"cash":        cash,
		"slug_prefix": slugPrefix,
	}, 200)
}

func (cs *ControlServer) handleReload(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		sendJSON(w, map[string]interface{}{"error": "Method not allowed"}, 405)
		return
	}
	body, err := readJSONBody(r)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	slugPrefix, ok := requireSlug(body, w)
	if !ok {
		return
	}
	strategyName, _ := body["strategy"].(string)
	if strategyName == "" {
		sendJSON(w, map[string]interface{}{"error": "Missing 'strategy' field"}, 400)
		return
	}

	runner, err := cs.manager.GetMarket(slugPrefix)
	if err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}

	// Find the worker to get its config.
	runner.mu.Lock()
	var target *WorkerInfo
	for _, w := range runner.workers {
		if w.StrategyName == strategyName {
			target = w
			break
		}
	}
	runner.mu.Unlock()

	if target == nil {
		sendJSON(w, map[string]interface{}{"error": fmt.Sprintf("No active worker for '%s'", strategyName)}, 404)
		return
	}

	state := runner.readWorkerState(strategyName)
	cash := target.Cash
	if state != nil {
		if c, ok := state["cash"].(float64); ok {
			cash = c
		}
	}

	if err := runner.EnableTrading(strategyName, target.Params, &cash,
		target.DryRun, target.PerSession, target.SessionCap); err != nil {
		sendJSON(w, map[string]interface{}{"error": err.Error()}, 400)
		return
	}
	sendJSON(w, map[string]interface{}{
		"ok":          true,
		"strategy":    strategyName,
		"reloaded":    true,
		"slug_prefix": slugPrefix,
	}, 200)
}
