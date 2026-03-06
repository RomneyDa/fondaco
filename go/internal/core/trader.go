package core

import (
	"fmt"
	"log"
	"math"
	"time"
)

// ---------------------------------------------------------------------------
// Core types
// ---------------------------------------------------------------------------

// TradeAction is the output from a strategy's OnTick method.
type TradeAction struct {
	Side              string   `json:"side"` // buy_up, buy_down, sell_up, sell_down, hold, cancel_up, cancel_down
	Amount            float64  `json:"amount"`
	Price             *float64 `json:"price,omitempty"`
	ExpirationSeconds *float64 `json:"expiration_seconds,omitempty"`
}

// Hold is the no-op trade action.
var Hold = TradeAction{Side: "hold", Amount: 0.0}

// OpenOrder tracks a pending GTC/GTD limit order.
type OpenOrder struct {
	OrderID         string   `json:"order_id"`
	Side            string   `json:"side"`
	Token           string   `json:"token"` // "up" or "down"
	Price           float64  `json:"price"`
	Tokens          float64  `json:"tokens"`
	DollarsReserved float64  `json:"dollars_reserved"`
	TokensReserved  float64  `json:"tokens_reserved"`
	TickPlaced      int      `json:"tick_placed"`
	TokenID         string   `json:"token_id,omitempty"`
	Expiration      *float64 `json:"expiration,omitempty"` // sim: elapsed_seconds deadline; live: unix timestamp
}

// Trade records an executed trade (simulation or live).
type Trade struct {
	TickIndex       int      `json:"tick_index"`
	Side            string   `json:"side"`
	Price           float64  `json:"price"`
	Amount          float64  `json:"amount"`
	Tokens          float64  `json:"tokens"`
	OrderID         string   `json:"order_id,omitempty"`
	TokenID         string   `json:"token_id,omitempty"`
	Success         *bool    `json:"success,omitempty"`
	Error           string   `json:"error,omitempty"`
	Timestamp       string   `json:"timestamp,omitempty"`
	RequestedTokens *float64 `json:"requested_tokens,omitempty"`
	ElapsedSeconds  *float64 `json:"elapsed_seconds,omitempty"`
	OrderType       string   `json:"order_type,omitempty"`
}

// ---------------------------------------------------------------------------
// Position
// ---------------------------------------------------------------------------

// Position holds current cash and token balances.
type Position struct {
	Cash       float64 `json:"cash"`
	UpTokens   float64 `json:"up_tokens"`
	DownTokens float64 `json:"down_tokens"`
}

// ComputeTradeSize computes the tokens and dollar amount for a trade.
//
// For buys:  amount = dollars to spend. Returns (tokens_to_buy, dollars_to_spend).
// For sells: amount = max tokens to sell. Returns (tokens_to_sell, proceeds).
//
// Returns (0, 0) if the trade cannot execute (insufficient balance, price out of
// range, or below minimum order size).
func (p *Position) ComputeTradeSize(side string, price float64, amount float64) (tokens float64, dollars float64) {
	if price <= 0 || price >= 1.0 {
		return 0, 0
	}

	switch side {
	case "buy_up", "buy_down":
		d := math.Min(amount, p.Cash)
		if d <= 0 {
			return 0, 0
		}
		tk := math.Round(d/price*10000) / 10000 // round to 4 decimals
		if tk < ClobMinOrderTokens {
			return 0, 0
		}
		cost := tk * price
		return tk, cost

	case "sell_up", "sell_down":
		var available float64
		if side == "sell_up" {
			available = p.UpTokens
		} else {
			available = p.DownTokens
		}
		tk := math.Floor(math.Min(amount, available)*10000) / 10000
		if tk <= 0 {
			return 0, 0
		}
		proceeds := tk * price
		return tk, proceeds
	}

	return 0, 0
}

// ApplyTrade mutates the position for a completed trade.
// For buys: deducts cost from cash, adds tokens.
// For sells: removes tokens, adds proceeds to cash.
func (p *Position) ApplyTrade(side string, price float64, tokens float64) {
	if tokens <= 0 {
		return
	}
	switch side {
	case "buy_up":
		p.Cash -= tokens * price
		p.UpTokens += tokens
	case "buy_down":
		p.Cash -= tokens * price
		p.DownTokens += tokens
	case "sell_up":
		p.UpTokens -= tokens
		p.Cash += tokens * price
	case "sell_down":
		p.DownTokens -= tokens
		p.Cash += tokens * price
	}

	if p.Cash < -0.01 || p.UpTokens < -0.0001 || p.DownTokens < -0.0001 {
		log.Printf("POSITION INTEGRITY VIOLATION: cash=%.4f up=%.4f down=%.4f after %s %.4f tokens @ %.4f",
			p.Cash, p.UpTokens, p.DownTokens, side, tokens, price)
	}
}

// Resolve resolves the position at session end. Winning tokens pay $1 each,
// losing tokens pay $0. If outcome is empty (timeout), position is unchanged.
func (p *Position) Resolve(outcome string) {
	switch outcome {
	case "Up":
		p.Cash += p.UpTokens
		p.UpTokens = 0
		p.DownTokens = 0
	case "Down":
		p.Cash += p.DownTokens
		p.UpTokens = 0
		p.DownTokens = 0
	}
}

// ReserveForOrder deducts cash (buys) or tokens (sells) immediately so the
// strategy cannot double-spend while a limit order is pending.
// Returns (dollarsReserved, tokensReserved).
func (p *Position) ReserveForOrder(side string, price float64, tokens float64) (float64, float64) {
	switch side {
	case "buy_up", "buy_down":
		cost := tokens * price
		p.Cash -= cost
		return cost, 0
	case "sell_up":
		p.UpTokens -= tokens
		return 0, tokens
	case "sell_down":
		p.DownTokens -= tokens
		return 0, tokens
	}
	return 0, 0
}

// UnreserveForOrder returns reserved funds when a limit order is cancelled.
func (p *Position) UnreserveForOrder(side string, dollarsReserved float64, tokensReserved float64) {
	switch side {
	case "buy_up", "buy_down":
		p.Cash += dollarsReserved
	case "sell_up":
		p.UpTokens += tokensReserved
	case "sell_down":
		p.DownTokens += tokensReserved
	}
}

// ToDict returns a map representation of the position.
func (p *Position) ToDict() map[string]interface{} {
	return map[string]interface{}{
		"cash":        p.Cash,
		"up_tokens":   p.UpTokens,
		"down_tokens": p.DownTokens,
	}
}

// PositionFromDict creates a Position from a map (e.g., from JSON).
func PositionFromDict(d map[string]interface{}) *Position {
	p := &Position{}
	if v, ok := d["cash"].(float64); ok {
		p.Cash = v
	}
	if v, ok := d["up_tokens"].(float64); ok {
		p.UpTokens = v
	}
	if v, ok := d["down_tokens"].(float64); ok {
		p.DownTokens = v
	}
	return p
}

// ---------------------------------------------------------------------------
// TickContext
// ---------------------------------------------------------------------------

// TickContext is everything a strategy sees at each tick.
type TickContext struct {
	TickIndex      int                      `json:"tick_index"`
	Sample         map[string]interface{}   `json:"sample"`
	History        []map[string]interface{} `json:"history"`
	Position       *Position                `json:"position"`
	Metadata       map[string]interface{}   `json:"metadata"`
	InitialBtcPrice float64                 `json:"initial_btc_price"`
	ElapsedFraction float64                 `json:"elapsed_fraction"`
	MaxBuyAmount    float64                 `json:"max_buy_amount"`
	LossLimitHit    bool                    `json:"loss_limit_hit"`
	OpenOrders      []OpenOrder             `json:"open_orders"`
}

// ---------------------------------------------------------------------------
// Strategy interface
// ---------------------------------------------------------------------------

// Strategy is the interface that all trading strategies must implement.
type Strategy interface {
	Name() string
	OnSessionStart(metadata map[string]interface{})
	OnTick(ctx *TickContext) TradeAction
	// TickLogs returns and clears the decision log messages for the current tick.
	TickLogs() []string
}

// ---------------------------------------------------------------------------
// Executor interface
// ---------------------------------------------------------------------------

// Executor is the abstract trade executor interface.
type Executor interface {
	// ExecuteTrade executes a market order (FAK). Returns a Trade on execution,
	// or nil if nothing to do.
	ExecuteTrade(action TradeAction, tickIndex int, metadata map[string]interface{},
		sample map[string]interface{}, position *Position) *Trade

	// PlaceLimitOrder posts a GTC/GTD limit order and reserves funds.
	// Returns an OpenOrder on success, or nil.
	PlaceLimitOrder(action TradeAction, tickIndex int, metadata map[string]interface{},
		sample map[string]interface{}, position *Position) *OpenOrder

	// CheckFills checks pending orders for fills. Modifies open_orders in place
	// (removes filled/expired orders). Returns fill Trade records.
	CheckFills(openOrders *[]OpenOrder, sample map[string]interface{}, position *Position) []Trade

	// CancelOrder cancels a pending order and unreserves funds.
	CancelOrder(order OpenOrder, position *Position) bool

	// CancelAllOrders cancels all pending orders. Returns count cancelled.
	CancelAllOrders(openOrders *[]OpenOrder, position *Position) int
}

// ---------------------------------------------------------------------------
// SimExecutor
// ---------------------------------------------------------------------------

// SimExecutor executes trades against historical sample data for backtesting.
type SimExecutor struct {
	ExecutionMode   string  // "book", "midpoint", "midpoint_slippage"
	Slippage        float64
	simOrderCounter int
}

// NewSimExecutor creates a SimExecutor with the given execution mode and slippage.
func NewSimExecutor(executionMode string, slippage float64) *SimExecutor {
	if executionMode == "" {
		executionMode = "book"
	}
	return &SimExecutor{
		ExecutionMode: executionMode,
		Slippage:      slippage,
	}
}

// ExecuteTrade executes a simulated market order against historical data.
func (e *SimExecutor) ExecuteTrade(action TradeAction, tickIndex int, metadata map[string]interface{},
	sample map[string]interface{}, position *Position) *Trade {

	if action.Side == "hold" || action.Amount <= 0 {
		return nil
	}

	price, ok := e.getExecutionPrice(action.Side, sample)
	if !ok {
		return nil
	}

	tokens, dollars := position.ComputeTradeSize(action.Side, price, action.Amount)
	if tokens <= 0 {
		return nil
	}
	if tokens < ClobMinOrderTokens {
		return nil
	}

	position.ApplyTrade(action.Side, price, tokens)

	var elapsed *float64
	if v, ok := sampleFloat(sample, "elapsed_seconds"); ok {
		elapsed = &v
	}

	return &Trade{
		TickIndex:      tickIndex,
		Side:           action.Side,
		Price:          price,
		Amount:         dollars,
		Tokens:         tokens,
		ElapsedSeconds: elapsed,
	}
}

// PlaceLimitOrder simulates posting a GTC/GTD limit order.
func (e *SimExecutor) PlaceLimitOrder(action TradeAction, tickIndex int, metadata map[string]interface{},
	sample map[string]interface{}, position *Position) *OpenOrder {

	if action.Side == "hold" || action.Amount <= 0 || action.Price == nil {
		return nil
	}

	price := math.Round(*action.Price*100) / 100 // round to 2 decimals
	if price <= 0 || price >= 1.0 {
		return nil
	}

	tokens, _ := position.ComputeTradeSize(action.Side, price, action.Amount)
	if tokens <= 0 || tokens < ClobMinOrderTokens {
		return nil
	}

	// For buys, floor to integer (same as ClobExecutor).
	if action.Side == "buy_up" || action.Side == "buy_down" {
		tokens = math.Floor(tokens)
		if tokens < ClobMinOrderTokens {
			return nil
		}
	}

	token := "down"
	if action.Side == "buy_up" || action.Side == "sell_up" {
		token = "up"
	}

	// Reserve funds.
	dollarsReserved, tokensReserved := position.ReserveForOrder(action.Side, price, tokens)

	// Compute GTD expiration deadline (elapsed_seconds).
	var expiration *float64
	if action.ExpirationSeconds != nil {
		currentElapsed, _ := sampleFloat(sample, "elapsed_seconds")
		exp := currentElapsed + *action.ExpirationSeconds
		expiration = &exp
	}

	e.simOrderCounter++
	return &OpenOrder{
		OrderID:         fmt.Sprintf("sim-%d", e.simOrderCounter),
		Side:            action.Side,
		Token:           token,
		Price:           price,
		Tokens:          tokens,
		DollarsReserved: dollarsReserved,
		TokensReserved:  tokensReserved,
		TickPlaced:      tickIndex,
		Expiration:      expiration,
	}
}

// CheckFills checks pending limit orders for simulated fills.
// Buy at P fills when midpoint <= P; sell at P fills when midpoint >= P.
func (e *SimExecutor) CheckFills(openOrders *[]OpenOrder, sample map[string]interface{}, position *Position) []Trade {
	var fills []Trade
	var remaining []OpenOrder
	currentElapsed, _ := sampleFloat(sample, "elapsed_seconds")

	for _, order := range *openOrders {
		// GTD expiration check.
		if order.Expiration != nil && currentElapsed >= *order.Expiration {
			position.UnreserveForOrder(order.Side, order.DollarsReserved, order.TokensReserved)
			continue
		}

		midKey := "down_token_midpoint"
		if order.Token == "up" {
			midKey = "up_token_midpoint"
		}
		midpoint, ok := sampleFloat(sample, midKey)
		if !ok {
			remaining = append(remaining, order)
			continue
		}

		filled := false
		switch order.Side {
		case "buy_up", "buy_down":
			filled = midpoint <= order.Price
		case "sell_up", "sell_down":
			filled = midpoint >= order.Price
		}

		if filled {
			// Apply fill: funds were already reserved.
			switch order.Side {
			case "buy_up":
				position.UpTokens += order.Tokens
			case "buy_down":
				position.DownTokens += order.Tokens
			case "sell_up", "sell_down":
				position.Cash += order.Tokens * order.Price
			}

			orderType := "GTC"
			if order.Expiration != nil {
				orderType = "GTD"
			}

			var elapsed *float64
			if v, ok := sampleFloat(sample, "elapsed_seconds"); ok {
				elapsed = &v
			}

			fills = append(fills, Trade{
				TickIndex:      0, // will be set by caller
				Side:           order.Side,
				Price:          order.Price,
				Amount:         order.Tokens * order.Price,
				Tokens:         order.Tokens,
				OrderID:        order.OrderID,
				OrderType:      orderType,
				ElapsedSeconds: elapsed,
			})
		} else {
			remaining = append(remaining, order)
		}
	}

	*openOrders = remaining
	return fills
}

// CancelOrder cancels a simulated order, returning reserved funds.
func (e *SimExecutor) CancelOrder(order OpenOrder, position *Position) bool {
	position.UnreserveForOrder(order.Side, order.DollarsReserved, order.TokensReserved)
	return true
}

// CancelAllOrders cancels all pending simulated orders.
func (e *SimExecutor) CancelAllOrders(openOrders *[]OpenOrder, position *Position) int {
	cancelled := 0
	for _, order := range *openOrders {
		if e.CancelOrder(order, position) {
			cancelled++
		}
	}
	*openOrders = nil
	return cancelled
}

// getExecutionPrice looks up the execution price based on mode and action side.
func (e *SimExecutor) getExecutionPrice(side string, sample map[string]interface{}) (float64, bool) {
	switch e.ExecutionMode {
	case "midpoint_slippage":
		keys := map[string]string{
			"buy_up":    "up_token_midpoint",
			"sell_up":   "up_token_midpoint",
			"buy_down":  "down_token_midpoint",
			"sell_down": "down_token_midpoint",
		}
		key, ok := keys[side]
		if !ok {
			return 0, false
		}
		base, ok := sampleFloat(sample, key)
		if !ok {
			return 0, false
		}
		if side == "buy_up" || side == "buy_down" {
			return base + e.Slippage, true
		}
		return base - e.Slippage, true

	case "midpoint":
		keys := map[string]string{
			"buy_up":    "up_token_midpoint",
			"sell_up":   "up_token_midpoint",
			"buy_down":  "down_token_midpoint",
			"sell_down": "down_token_midpoint",
		}
		key, ok := keys[side]
		if !ok {
			return 0, false
		}
		return sampleFloat(sample, key)

	case "book":
		keys := map[string]string{
			"buy_up":    "up_token_best_ask",
			"sell_up":   "up_token_midpoint",
			"buy_down":  "down_token_best_ask",
			"sell_down": "down_token_midpoint",
		}
		key, ok := keys[side]
		if !ok {
			return 0, false
		}
		return sampleFloat(sample, key)
	}

	return 0, false
}

// ---------------------------------------------------------------------------
// Trader
// ---------------------------------------------------------------------------

// SettlementCooldownSeconds is the time to wait after a buy fill before
// allowing sells (live trading only, skipped in simulation).
const SettlementCooldownSeconds = 10.0

// Trader drives the tick loop identically for simulation and live trading.
type Trader struct {
	strategy Strategy
	executor Executor

	metadata          map[string]interface{}
	position          *Position
	initialBtcPrice   float64
	windowDuration    float64
	history           []map[string]interface{}
	trades            []Trade
	tickIndex         int
	captureDecisionLog bool
	decisionLog       []map[string]interface{}
	openOrders        []OpenOrder
	lastTickFills     []Trade
	settlementCooldowns map[string]float64 // token -> unix time of last buy fill

	// isSimExecutor is true when the executor is a *SimExecutor.
	// Settlement cooldown is skipped in simulation.
	isSimExecutor bool
}

// NewTrader creates a Trader with the given strategy and executor.
func NewTrader(strategy Strategy, executor Executor) *Trader {
	_, isSim := executor.(*SimExecutor)
	return &Trader{
		strategy:            strategy,
		executor:            executor,
		isSimExecutor:       isSim,
		settlementCooldowns: make(map[string]float64),
	}
}

// StartSession resets state and notifies the strategy of a new session.
func (t *Trader) StartSession(metadata map[string]interface{}, position *Position,
	initialBtcPrice float64, windowDuration float64,
	referenceBalance float64, captureDecisionLog bool) {

	t.metadata = metadata
	t.position = position
	t.initialBtcPrice = initialBtcPrice
	t.windowDuration = windowDuration
	t.history = nil
	t.trades = nil
	t.tickIndex = 0
	t.captureDecisionLog = captureDecisionLog
	t.decisionLog = nil
	t.openOrders = nil
	t.lastTickFills = nil
	t.settlementCooldowns = make(map[string]float64)

	// Clear strategy tick logs and notify session start.
	t.strategy.TickLogs() // drain any leftover
	t.strategy.OnSessionStart(metadata)
}

// ProcessTick builds a TickContext, calls the strategy, executes the trade,
// and updates history. Returns the Trade (or nil).
// For limit order fills, use LastTickFills().
func (t *Trader) ProcessTick(sample map[string]interface{}) *Trade {
	// 1. Check fills on pending limit orders (before strategy runs).
	t.lastTickFills = nil
	if len(t.openOrders) > 0 {
		fills := t.executor.CheckFills(&t.openOrders, sample, t.position)
		for i := range fills {
			fills[i].TickIndex = t.tickIndex
			t.trades = append(t.trades, fills[i])
		}
		t.lastTickFills = fills

		// Track settlement cooldowns for buy fills.
		for _, fill := range fills {
			if fill.Side == "buy_up" || fill.Side == "buy_down" {
				token := "up"
				if fill.Side == "buy_down" {
					token = "down"
				}
				t.settlementCooldowns[token] = float64(time.Now().Unix())
			}
		}
	}

	// Compute elapsed fraction.
	elapsedFrac := 0.0
	if t.windowDuration > 0 {
		if es, ok := sampleFloat(sample, "elapsed_seconds"); ok {
			elapsedFrac = es / t.windowDuration
		}
	}
	if elapsedFrac > 1.0 {
		elapsedFrac = 1.0
	}

	// 2. Build TickContext with a snapshot of the position.
	ctx := &TickContext{
		TickIndex: t.tickIndex,
		Sample:    sample,
		History:   t.history,
		Position: &Position{
			Cash:       t.position.Cash,
			UpTokens:   t.position.UpTokens,
			DownTokens: t.position.DownTokens,
		},
		Metadata:        t.metadata,
		InitialBtcPrice: t.initialBtcPrice,
		ElapsedFraction: elapsedFrac,
		MaxBuyAmount:    t.position.Cash,
		LossLimitHit:    false,
		OpenOrders:      append([]OpenOrder(nil), t.openOrders...),
	}

	// 3. Call strategy.
	action := t.strategy.OnTick(ctx)

	// Drain strategy decision log buffer.
	tickLogs := t.strategy.TickLogs()
	if len(tickLogs) > 0 && t.captureDecisionLog {
		t.decisionLog = append(t.decisionLog, map[string]interface{}{
			"tick": t.tickIndex,
			"msgs": tickLogs,
		})
	}

	// 4. Apply guardrails.
	action = t.applyGuardrails(action, sample)

	// 5. Route cancel actions.
	if len(action.Side) > 6 && action.Side[:6] == "cancel" {
		token := "down"
		if action.Side == "cancel_up" {
			token = "up"
		}
		var kept []OpenOrder
		for _, order := range t.openOrders {
			if order.Token == token {
				t.executor.CancelOrder(order, t.position)
			} else {
				kept = append(kept, order)
			}
		}
		t.openOrders = kept
		t.history = append(t.history, sample)
		t.tickIndex++
		return nil
	}

	// 6. Route: limit order vs FAK.
	var trade *Trade
	if action.Side != "hold" && action.Price != nil {
		// Limit order path: cancel existing order on same token first.
		t.replaceOrderOnToken(action)
		order := t.executor.PlaceLimitOrder(action, t.tickIndex, t.metadata, sample, t.position)
		if order != nil {
			t.openOrders = append(t.openOrders, *order)
		}
	} else {
		// FAK path.
		trade = t.executor.ExecuteTrade(action, t.tickIndex, t.metadata, sample, t.position)
	}

	// Track successful trades (success is nil for sim, true for live).
	if trade != nil && (trade.Success == nil || *trade.Success) {
		t.trades = append(t.trades, *trade)
	}

	t.history = append(t.history, sample)
	t.tickIndex++
	return trade
}

// EndSession cancels pending orders, resolves the position, and returns all trades.
func (t *Trader) EndSession(outcome string) []Trade {
	t.CancelAllPending()
	t.position.Resolve(outcome)
	result := make([]Trade, len(t.trades))
	copy(result, t.trades)
	return result
}

// CancelAllPending cancels all pending limit orders. Returns count cancelled.
func (t *Trader) CancelAllPending() int {
	if len(t.openOrders) == 0 {
		return 0
	}
	return t.executor.CancelAllOrders(&t.openOrders, t.position)
}

// LastTickFills returns limit order fills from the most recent ProcessTick call.
func (t *Trader) LastTickFills() []Trade {
	return t.lastTickFills
}

// DecisionLog returns the sparse decision log (only ticks where strategy called log).
func (t *Trader) DecisionLog() []map[string]interface{} {
	return t.decisionLog
}

// applyGuardrails applies settlement cooldown and caps buys to available cash.
func (t *Trader) applyGuardrails(action TradeAction, sample map[string]interface{}) TradeAction {
	if action.Side == "hold" {
		return action
	}

	// Settlement cooldown (live only, not sim).
	if (action.Side == "sell_up" || action.Side == "sell_down") && !t.isSimExecutor {
		token := "up"
		if action.Side == "sell_down" {
			token = "down"
		}
		if fillTime, ok := t.settlementCooldowns[token]; ok {
			elapsed := float64(time.Now().Unix()) - fillTime
			if elapsed < SettlementCooldownSeconds {
				log.Printf("Settlement cooldown: suppressing %s (%.1fs since fill, need %.1fs)",
					action.Side, elapsed, SettlementCooldownSeconds)
				return Hold
			}
			delete(t.settlementCooldowns, token)
		}
	}

	// Cancel actions pass through.
	if len(action.Side) >= 6 && action.Side[:6] == "cancel" {
		return action
	}

	// Cap buys to available cash.
	if action.Side == "buy_up" || action.Side == "buy_down" {
		cash := t.position.Cash
		if cash <= 0 {
			return Hold
		}
		if action.Amount > cash {
			action = TradeAction{
				Side:              action.Side,
				Amount:            cash,
				Price:             action.Price,
				ExpirationSeconds: action.ExpirationSeconds,
			}
		}
	}

	return action
}

// replaceOrderOnToken cancels any existing pending order on the same token
// before placing a new one.
func (t *Trader) replaceOrderOnToken(action TradeAction) {
	token := "down"
	if action.Side == "buy_up" || action.Side == "sell_up" {
		token = "up"
	}
	var kept []OpenOrder
	for _, order := range t.openOrders {
		if order.Token == token {
			t.executor.CancelOrder(order, t.position)
		} else {
			kept = append(kept, order)
		}
	}
	t.openOrders = kept
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// sampleFloat extracts a float64 from a sample map. Returns (value, true) if
// present and numeric, (0, false) otherwise.
func sampleFloat(sample map[string]interface{}, key string) (float64, bool) {
	v, ok := sample[key]
	if !ok || v == nil {
		return 0, false
	}
	switch val := v.(type) {
	case float64:
		return val, true
	case int:
		return float64(val), true
	case int64:
		return float64(val), true
	}
	return 0, false
}

// Float64Ptr returns a pointer to the given float64 value.
func Float64Ptr(v float64) *float64 {
	return &v
}

// BoolPtr returns a pointer to the given bool value.
func BoolPtr(v bool) *bool {
	return &v
}
