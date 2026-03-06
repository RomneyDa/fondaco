// Package core - rtds_client.go
// WebSocket client for Chainlink prices from Polymarket RTDS.
// Ported from Python core/rtds_client.py.

package core

import (
	"encoding/json"
	"log"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

const (
	RTDSWebSocketURL       = "wss://ws-live-data.polymarket.com"
	rtdsReconnectDelay     = 2 * time.Second
	rtdsMaxReconnectDelay  = 30 * time.Second
	rtdsStaleThreshold     = 10 * time.Second
	rtdsWatchdogTimeout    = 30 * time.Second
)

// RTDSClient is a background WebSocket client for Polymarket RTDS Chainlink prices.
type RTDSClient struct {
	symbol      string
	latestPrice *float64
	latestTS    time.Time // monotonic-ish: time.Now()
	mu          sync.Mutex
	running     bool
	tickCount   int64
	ws          *websocket.Conn
	wsMu        sync.Mutex
	wakeEvent   chan struct{}
	connectTS   time.Time // zero means sleeping
	connectTSMu sync.Mutex
}

// NewRTDSClient creates a new RTDS client for the given symbol (e.g., "btc/usd").
func NewRTDSClient(symbol string) *RTDSClient {
	return &RTDSClient{
		symbol:    strings.ToLower(symbol),
		wakeEvent: make(chan struct{}, 1),
	}
}

// Start begins the background WebSocket thread and watchdog.
func (c *RTDSClient) Start() {
	c.mu.Lock()
	if c.running {
		c.mu.Unlock()
		return
	}
	c.running = true
	c.mu.Unlock()

	go c.runLoop()
	go c.watchdogLoop()
	log.Printf("RTDS client started for %s", c.symbol)
}

// Stop stops the background WebSocket thread.
func (c *RTDSClient) Stop() {
	c.mu.Lock()
	c.running = false
	c.mu.Unlock()

	// Wake the reconnect sleep.
	select {
	case c.wakeEvent <- struct{}{}:
	default:
	}

	c.wsMu.Lock()
	if c.ws != nil {
		c.ws.Close()
	}
	c.wsMu.Unlock()
	log.Printf("RTDS client stopped")
}

// GetPrice returns the latest price if fresh enough, else nil.
func (c *RTDSClient) GetPrice(maxAge time.Duration) *float64 {
	if maxAge <= 0 {
		maxAge = rtdsStaleThreshold
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.latestPrice == nil {
		return nil
	}
	age := time.Since(c.latestTS)
	if age > maxAge {
		log.Printf("RTDS price stale (%.1fs old)", age.Seconds())
		return nil
	}
	v := *c.latestPrice
	return &v
}

// TickCount returns the number of price ticks received since start.
func (c *RTDSClient) TickCount() int64 {
	return atomic.LoadInt64(&c.tickCount)
}

func (c *RTDSClient) isRunning() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.running
}

func (c *RTDSClient) watchdogLoop() {
	for c.isRunning() {
		time.Sleep(5 * time.Second)

		c.mu.Lock()
		var age time.Duration
		if !c.latestTS.IsZero() {
			age = time.Since(c.latestTS)
		}
		c.mu.Unlock()

		if age <= rtdsWatchdogTimeout {
			continue
		}

		c.connectTSMu.Lock()
		cts := c.connectTS
		c.connectTSMu.Unlock()

		if !cts.IsZero() {
			// Inside connect
			connectAge := time.Since(cts)
			if connectAge < rtdsWatchdogTimeout {
				continue
			}
			log.Printf("RTDS watchdog: no ticks for %.0fs, forcing reconnect", age.Seconds())
			c.wsMu.Lock()
			if c.ws != nil {
				c.ws.Close()
			}
			c.wsMu.Unlock()
		} else {
			// Reconnect loop is sleeping - wake it up.
			select {
			case c.wakeEvent <- struct{}{}:
			default:
			}
		}
	}
}

func (c *RTDSClient) runLoop() {
	delay := rtdsReconnectDelay
	for c.isRunning() {
		// Clear wake event.
		select {
		case <-c.wakeEvent:
		default:
		}

		ticksBefore := atomic.LoadInt64(&c.tickCount)

		c.connectTSMu.Lock()
		c.connectTS = time.Now()
		c.connectTSMu.Unlock()

		c.connect()

		c.connectTSMu.Lock()
		c.connectTS = time.Time{}
		c.connectTSMu.Unlock()

		if !c.isRunning() {
			break
		}

		// Reset backoff if we received ticks during this connection.
		if atomic.LoadInt64(&c.tickCount) > ticksBefore {
			delay = rtdsReconnectDelay
		} else {
			delay = time.Duration(float64(delay) * 1.5)
			if delay > rtdsMaxReconnectDelay {
				delay = rtdsMaxReconnectDelay
			}
		}

		log.Printf("RTDS reconnecting in %.1fs...", delay.Seconds())
		select {
		case <-c.wakeEvent:
		case <-time.After(delay):
		}
	}
}

func (c *RTDSClient) connect() {
	dialer := websocket.Dialer{
		HandshakeTimeout: 10 * time.Second,
	}
	conn, _, err := dialer.Dial(RTDSWebSocketURL, nil)
	if err != nil {
		log.Printf("RTDS connection error: %v", err)
		return
	}

	c.wsMu.Lock()
	c.ws = conn
	c.wsMu.Unlock()

	defer func() {
		conn.Close()
		c.wsMu.Lock()
		if c.ws == conn {
			c.ws = nil
		}
		c.wsMu.Unlock()
	}()

	// Subscribe to all chainlink symbols.
	subscribeMsg := map[string]interface{}{
		"action": "subscribe",
		"subscriptions": []map[string]interface{}{
			{
				"topic":   "crypto_prices_chainlink",
				"type":    "*",
				"filters": "",
			},
		},
	}
	data, _ := json.Marshal(subscribeMsg)
	if err := conn.WriteMessage(websocket.TextMessage, data); err != nil {
		log.Printf("RTDS subscribe error: %v", err)
		return
	}
	log.Printf("RTDS subscribed to Chainlink (all symbols)")

	// Set up ping handler.
	conn.SetPongHandler(func(string) error {
		return nil
	})

	// Start ping goroutine.
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if err := conn.WriteMessage(websocket.PingMessage, nil); err != nil {
					return
				}
			case <-done:
				return
			}
		}
	}()
	defer close(done)

	// Read messages.
	for c.isRunning() {
		_, message, err := conn.ReadMessage()
		if err != nil {
			log.Printf("RTDS WebSocket read error: %v", err)
			return
		}
		if len(message) == 0 {
			continue
		}

		var msg map[string]interface{}
		if err := json.Unmarshal(message, &msg); err != nil {
			continue
		}

		topic, _ := msg["topic"].(string)
		if topic != "crypto_prices_chainlink" {
			continue
		}

		payload, _ := msg["payload"].(map[string]interface{})
		if payload == nil {
			continue
		}

		// Handle single-tick messages.
		if _, hasSymbol := payload["symbol"]; hasSymbol {
			c.handleTick(payload)
			continue
		}

		// Handle batch messages.
		if dataArr, ok := payload["data"].([]interface{}); ok {
			for _, item := range dataArr {
				if tick, ok := item.(map[string]interface{}); ok {
					c.handleTick(tick)
				}
			}
		}
	}
}

func (c *RTDSClient) handleTick(tick map[string]interface{}) {
	sym, _ := tick["symbol"].(string)
	if sym != "" && strings.ToLower(sym) != c.symbol {
		return
	}

	var price float64
	switch v := tick["value"].(type) {
	case float64:
		price = v
	case string:
		// Try to parse string value.
		p, err := strconv.ParseFloat(v, 64)
		if err != nil {
			return
		}
		price = p
	default:
		return
	}

	c.mu.Lock()
	c.latestPrice = &price
	c.latestTS = time.Now()
	c.mu.Unlock()

	count := atomic.AddInt64(&c.tickCount, 1)
	if count <= 3 || count%60 == 0 {
		log.Printf("RTDS %s tick #%d: $%.2f", c.symbol, count, price)
	}
}
