// cmd/app/main.go
// CLI entry point for the Polymarket multi-market app.
// Ported from Python app/__main__.py.

package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/polymarket/bitcoin-5-minute/internal/app"
	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

func main() {
	if len(os.Args) < 2 {
		printUsage()
		os.Exit(1)
	}

	command := os.Args[1]
	args := os.Args[2:]

	switch command {
	case "start":
		cmdStart(args)
	case "market-on":
		cmdMarketOn(args)
	case "market-off":
		cmdMarketOff(args)
	case "trade-on":
		cmdTradeOn(args)
	case "trade-off":
		cmdTradeOff(args)
	case "set-cash":
		cmdSetCash(args)
	case "reload":
		cmdReload(args)
	case "kill":
		cmdKill()
	case "status":
		cmdStatus()
	case "stop":
		cmdStop()
	case "claim":
		cmdClaim(args)
	default:
		fmt.Printf("Unknown command: %s\n", command)
		printUsage()
		os.Exit(1)
	}
}

func printUsage() {
	fmt.Println(`Polymarket Multi-Market App

Usage:
  app start [--port PORT] [--restore]
  app market-on SLUG [--window SECS] [--market-type TYPE] [--restore]
  app market-off SLUG [--after-session]
  app trade-on SLUG --strategy NAME [--params K=V,...] [--cash AMOUNT] [--dry-run] [--per-session] [--session-cap N]
  app trade-off SLUG [--strategy NAME]
  app set-cash SLUG AMOUNT [--strategy NAME]
  app reload SLUG --strategy NAME
  app kill
  app status
  app stop
  app claim [--no-wait]`)
}

// --- State file helpers ---

func readState() map[string]interface{} {
	stateFile := core.AppStateFile()
	data, err := os.ReadFile(stateFile)
	if err != nil {
		return nil
	}
	var state map[string]interface{}
	if json.Unmarshal(data, &state) != nil {
		return nil
	}
	pid, ok := state["pid"].(float64)
	if !ok {
		return nil
	}
	if !pidAlive(int(pid)) {
		os.Remove(stateFile)
		return nil
	}
	return state
}

func writeState(pid int, port int) {
	stateFile := core.AppStateFile()
	os.MkdirAll(strings.TrimSuffix(stateFile, "/app.json"), 0o755)
	data, _ := json.MarshalIndent(map[string]interface{}{
		"pid":  pid,
		"port": port,
	}, "", "  ")
	os.WriteFile(stateFile, data, 0o644)
}

func removeState() {
	os.Remove(core.AppStateFile())
}

func pidAlive(pid int) bool {
	process, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return process.Signal(syscall.Signal(0)) == nil
}

func httpRequest(method, path string, body map[string]interface{}) (map[string]interface{}, error) {
	state := readState()
	if state == nil {
		fmt.Println("App is not running.")
		os.Exit(1)
	}
	port := int(state["port"].(float64))
	url := fmt.Sprintf("http://localhost:%d%s", port, path)

	var reqBody io.Reader
	if body != nil {
		data, _ := json.Marshal(body)
		reqBody = strings.NewReader(string(data))
	}

	req, err := http.NewRequest(method, url, reqBody)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		fmt.Printf("Failed to connect to control server on port %d: %v\n", port, err)
		os.Exit(1)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)
	var result map[string]interface{}
	json.Unmarshal(respBody, &result)
	return result, nil
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
		// Try int, then float, then string.
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

func getFlag(args []string, name string) (string, []string) {
	for i, a := range args {
		if a == name && i+1 < len(args) {
			remaining := make([]string, 0, len(args)-2)
			remaining = append(remaining, args[:i]...)
			remaining = append(remaining, args[i+2:]...)
			return args[i+1], remaining
		}
		if strings.HasPrefix(a, name+"=") {
			remaining := make([]string, 0, len(args)-1)
			remaining = append(remaining, args[:i]...)
			remaining = append(remaining, args[i+1:]...)
			return strings.TrimPrefix(a, name+"="), remaining
		}
	}
	return "", args
}

func hasFlag(args []string, name string) (bool, []string) {
	for i, a := range args {
		if a == name {
			remaining := make([]string, 0, len(args)-1)
			remaining = append(remaining, args[:i]...)
			remaining = append(remaining, args[i+1:]...)
			return true, remaining
		}
	}
	return false, args
}

// --- Commands ---

func cmdStart(args []string) {
	state := readState()
	if state != nil {
		fmt.Printf("App already running (PID %.0f). Use 'app stop'.\n", state["pid"].(float64))
		os.Exit(1)
	}

	portStr, args := getFlag(args, "--port")
	port := core.DefaultControlPort
	if portStr != "" {
		if p, err := strconv.Atoi(portStr); err == nil {
			port = p
		}
	}
	restore, _ := hasFlag(args, "--restore")

	mgr := app.NewAppManager(port)
	server := app.NewControlServer(mgr, port)
	server.Start()

	writeState(os.Getpid(), port)

	if restore {
		mgr.Restore()
	}

	defer func() {
		server.Stop()
		removeState()
	}()

	mgr.Run()
}

func cmdMarketOn(args []string) {
	if len(args) < 1 {
		fmt.Println("Usage: app market-on SLUG [--window SECS] [--market-type TYPE] [--restore]")
		os.Exit(1)
	}
	slugPrefix := args[0]
	args = args[1:]

	body := map[string]interface{}{
		"slug_prefix": slugPrefix,
	}

	windowStr, args := getFlag(args, "--window")
	if windowStr != "" {
		if w, err := strconv.Atoi(windowStr); err == nil {
			body["window"] = w
		}
	}
	marketType, args := getFlag(args, "--market-type")
	if marketType != "" {
		body["market_type"] = marketType
	}
	restore, _ := hasFlag(args, "--restore")
	if restore {
		body["restore"] = true
	}

	resp, _ := httpRequest("POST", "/api/control/market-on", body)
	if ok, _ := resp["ok"].(bool); ok {
		fmt.Printf("Market started: %v\n", resp["slug_key"])
	} else {
		fmt.Printf("Error: %v\n", resp["error"])
		os.Exit(1)
	}
}

func cmdMarketOff(args []string) {
	if len(args) < 1 {
		fmt.Println("Usage: app market-off SLUG [--after-session]")
		os.Exit(1)
	}
	slugPrefix := args[0]
	args = args[1:]

	body := map[string]interface{}{
		"slug_prefix": slugPrefix,
	}
	afterSession, _ := hasFlag(args, "--after-session")
	if afterSession {
		body["after_session"] = true
	}

	resp, _ := httpRequest("POST", "/api/control/market-off", body)
	if ok, _ := resp["ok"].(bool); ok {
		fmt.Printf("Market stopped: %s\n", slugPrefix)
	} else {
		fmt.Printf("Error: %v\n", resp["error"])
		os.Exit(1)
	}
}

func cmdTradeOn(args []string) {
	if len(args) < 1 {
		fmt.Println("Usage: app trade-on SLUG --strategy NAME [--params K=V,...] [--cash AMOUNT] [--dry-run] [--per-session] [--session-cap N]")
		os.Exit(1)
	}
	slugPrefix := args[0]
	args = args[1:]

	body := map[string]interface{}{
		"slug_prefix": slugPrefix,
	}

	strategy, args := getFlag(args, "--strategy")
	if strategy == "" {
		fmt.Println("Error: --strategy is required")
		os.Exit(1)
	}
	body["strategy"] = strategy

	paramsStr, args := getFlag(args, "--params")
	if paramsStr != "" {
		body["params"] = parseParams(paramsStr)
	}

	cashStr, args := getFlag(args, "--cash")
	if cashStr != "" {
		if cashStr == "cached" {
			// TODO: implement cached balance loading
			fmt.Println("'cached' balance not yet implemented in Go")
			os.Exit(1)
		}
		if c, err := strconv.ParseFloat(cashStr, 64); err == nil {
			body["cash"] = c
		}
	}

	dryRun, args := hasFlag(args, "--dry-run")
	if dryRun {
		body["dry_run"] = true
	}
	perSession, args := hasFlag(args, "--per-session")
	if perSession {
		body["per_session"] = true
	}
	sessionCapStr, _ := getFlag(args, "--session-cap")
	if sessionCapStr != "" {
		if sc, err := strconv.ParseFloat(sessionCapStr, 64); err == nil {
			body["session_cap"] = sc
		}
	}

	resp, _ := httpRequest("POST", "/api/control/trade-on", body)
	if ok, _ := resp["ok"].(bool); ok {
		fmt.Printf("Trading enabled: strategy=%v on %v\n", resp["strategy"], resp["slug_prefix"])
	} else {
		fmt.Printf("Error: %v\n", resp["error"])
		os.Exit(1)
	}
}

func cmdTradeOff(args []string) {
	if len(args) < 1 {
		fmt.Println("Usage: app trade-off SLUG [--strategy NAME]")
		os.Exit(1)
	}
	slugPrefix := args[0]
	args = args[1:]

	body := map[string]interface{}{
		"slug_prefix": slugPrefix,
	}
	strategy, _ := getFlag(args, "--strategy")
	if strategy != "" {
		body["strategy"] = strategy
	}

	resp, _ := httpRequest("POST", "/api/control/trade-off", body)
	if ok, _ := resp["ok"].(bool); ok {
		if strategy != "" {
			fmt.Printf("Trading disabled for %s on %s.\n", strategy, slugPrefix)
		} else {
			fmt.Printf("All trading disabled on %s.\n", slugPrefix)
		}
	} else {
		fmt.Printf("Error: %v\n", resp["error"])
		os.Exit(1)
	}
}

func cmdSetCash(args []string) {
	if len(args) < 2 {
		fmt.Println("Usage: app set-cash SLUG AMOUNT [--strategy NAME]")
		os.Exit(1)
	}
	slugPrefix := args[0]
	amount, err := strconv.ParseFloat(args[1], 64)
	if err != nil {
		fmt.Printf("Invalid amount: %s\n", args[1])
		os.Exit(1)
	}
	remaining := args[2:]

	body := map[string]interface{}{
		"slug_prefix": slugPrefix,
		"cash":        amount,
	}
	strategy, _ := getFlag(remaining, "--strategy")
	if strategy != "" {
		body["strategy"] = strategy
	}

	resp, _ := httpRequest("POST", "/api/control/set-cash", body)
	if ok, _ := resp["ok"].(bool); ok {
		fmt.Printf("Cash updated: $%v\n", resp["cash"])
	} else {
		fmt.Printf("Error: %v\n", resp["error"])
		os.Exit(1)
	}
}

func cmdReload(args []string) {
	if len(args) < 1 {
		fmt.Println("Usage: app reload SLUG --strategy NAME")
		os.Exit(1)
	}
	slugPrefix := args[0]
	args = args[1:]

	strategy, _ := getFlag(args, "--strategy")
	if strategy == "" {
		fmt.Println("Error: --strategy is required")
		os.Exit(1)
	}

	body := map[string]interface{}{
		"slug_prefix": slugPrefix,
		"strategy":    strategy,
	}
	resp, _ := httpRequest("POST", "/api/control/reload", body)
	if ok, _ := resp["ok"].(bool); ok {
		fmt.Printf("Reloaded worker: %s on %s\n", strategy, slugPrefix)
	} else {
		fmt.Printf("Error: %v\n", resp["error"])
		os.Exit(1)
	}
}

func cmdKill() {
	resp, _ := httpRequest("POST", "/api/control/kill", nil)
	if ok, _ := resp["ok"].(bool); ok {
		fmt.Println("Kill signal sent -- all markets will stop after their current sessions.")
	} else {
		fmt.Printf("Error: %v\n", resp["error"])
		os.Exit(1)
	}
}

func cmdStatus() {
	state := readState()
	if state == nil {
		fmt.Println("App is not running.")
		return
	}

	resp, _ := httpRequest("GET", "/api/control/status", nil)
	fmt.Printf("App running (PID %.0f)\n", state["pid"].(float64))

	markets, ok := resp["markets"].([]interface{})
	if !ok || len(markets) == 0 {
		fmt.Println("  No markets active.")
		return
	}

	fmt.Printf("  Markets: %d\n", len(markets))
	for _, mRaw := range markets {
		m, ok := mRaw.(map[string]interface{})
		if !ok {
			continue
		}
		slug := ""
		if s, ok := m["slug_prefix"].(string); ok {
			slug = s
		} else if s, ok := m["slug_key"].(string); ok {
			slug = s
		}
		sessionCount, _ := m["session_count"].(float64)
		tickClients, _ := m["tick_clients"].(float64)
		stopTag := ""
		if stop, _ := m["stop_after_session"].(bool); stop {
			stopTag = " [STOPPING]"
		}

		fmt.Printf("  %s | sessions=%.0f | tick_clients=%.0f%s\n",
			slug, sessionCount, tickClients, stopTag)

		strategies, ok := m["strategies"].([]interface{})
		if !ok || len(strategies) == 0 {
			fmt.Println("    No workers")
			continue
		}
		for _, sRaw := range strategies {
			s, ok := sRaw.(map[string]interface{})
			if !ok {
				continue
			}
			name, _ := s["name"].(string)
			bankroll, _ := s["bankroll"].(float64)
			if bankroll == 0 {
				bankroll, _ = s["cash"].(float64)
			}
			dryTag := ""
			if dry, _ := s["dry_run"].(bool); dry {
				dryTag = " [DRY RUN]"
			}
			aliveTag := ""
			if alive, ok := s["alive"].(bool); ok && !alive {
				aliveTag = " [DEAD]"
			}
			pidTag := ""
			if pid, ok := s["pid"].(float64); ok {
				pidTag = fmt.Sprintf(" PID=%.0f", pid)
			}
			capInfo := ""
			if cap, ok := s["session_cap"].(float64); ok && cap > 0 {
				capInfo = fmt.Sprintf(" (cap=$%.2f)", cap)
			}
			params, _ := s["params"]
			fmt.Printf("    - %s | bankroll=$%.2f%s%s%s%s | params=%v\n",
				name, bankroll, capInfo, dryTag, pidTag, aliveTag, params)
		}
	}
}

func cmdStop() {
	state := readState()
	if state == nil {
		fmt.Println("App is not running.")
		return
	}

	pid := int(state["pid"].(float64))

	resp, err := httpRequest("POST", "/api/control/stop", nil)
	if err != nil || resp == nil {
		fmt.Printf("HTTP stop failed, sending SIGTERM to PID %d...\n", pid)
		if p, err := os.FindProcess(pid); err == nil {
			p.Signal(syscall.SIGTERM)
		}
	} else if ok, _ := resp["ok"].(bool); ok {
		fmt.Printf("Stop signal sent (PID %d). Waiting for graceful shutdown...\n", pid)
	}

	for i := 0; i < 30; i++ {
		if !pidAlive(pid) {
			fmt.Println("App stopped.")
			removeState()
			return
		}
		time.Sleep(1 * time.Second)
	}

	fmt.Printf("WARNING: App (PID %d) did not exit within 30s.\n", pid)
}

func cmdClaim(args []string) {
	privateKey := os.Getenv("POLYMARKET_PRIVATE_KEY")
	if privateKey == "" {
		fmt.Println("ERROR: POLYMARKET_PRIVATE_KEY environment variable not set.")
		os.Exit(1)
	}

	auth, err := app.GetAuth(privateKey, false)
	if err != nil {
		fmt.Printf("ERROR: Authentication failed: %v\n", err)
		os.Exit(1)
	}

	if !app.HasBuilderCreds() {
		fmt.Println("ERROR: Builder API credentials not set.")
		fmt.Println("Set POLYMARKET_BUILDER_API_KEY, POLYMARKET_BUILDER_SECRET, and POLYMARKET_BUILDER_PASSPHRASE in .env")
		os.Exit(1)
	}

	positions, err := app.FetchRedeemablePositions(auth.SafeAddress)
	if err != nil || len(positions) == 0 {
		fmt.Println("Nothing to claim.")
		return
	}

	fmt.Printf("\nRedeemable positions: %d\n", len(positions))

	noWait, _ := hasFlag(args, "--no-wait")
	waitForConfirm := !noWait

	succeeded, failed, totalValue, _ := app.ClaimRedeemable(privateKey, auth.SafeAddress, waitForConfirm)

	action := "confirmed"
	if !waitForConfirm {
		action = "submitted"
	}
	fmt.Printf("\nClaim complete: %d %s, %d failed\n", succeeded, action, failed)
	if succeeded > 0 && failed == 0 {
		fmt.Printf("Redeemed ~$%.2f USDC.e\n", totalValue)
	}
}
