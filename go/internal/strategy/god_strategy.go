package strategy

import (
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// GodStrategy is a contrarian cheap-token buyer using volatility and BTC
// direction. Buys the token opposite to BTC's current move. Entry score
// uses random-walk time decay:
//
//	score = volatility_bps * sqrt(1 - elapsed) / midpoint
//
// Takes profit at a linear target: tp_slope * elapsed + tp_intercept.
type GodStrategy struct {
	BaseStrategy
	LookbackSessions int
	MinSessions      int
	Threshold        float64
	MaxTokenPrice    float64
	MinElapsed       float64
	MinBtcMoveBps    float64
	MaxBtcMoveBps    float64
	TpSlope          float64
	TpIntercept      float64
	BetFraction      float64

	volatilityBps *float64
	bought        bool
	entrySide     string
	entryMidpoint float64
	tpTriggered   bool
}

// NewGodStrategy creates a GodStrategy from params.
func NewGodStrategy(params map[string]any) Strategy {
	return &GodStrategy{
		LookbackSessions: getInt(params, "lookback_sessions", 5),
		MinSessions:      getInt(params, "min_sessions", 5),
		Threshold:        getFloat(params, "threshold", 70.0),
		MaxTokenPrice:    getFloat(params, "max_token_price", 0.20),
		MinElapsed:       getFloat(params, "min_elapsed", 0.03),
		MinBtcMoveBps:    getFloat(params, "min_btc_move_bps", 1.0),
		MaxBtcMoveBps:    getFloat(params, "max_btc_move_bps", 20.0),
		TpSlope:          getFloat(params, "tp_slope", 1.25),
		TpIntercept:      getFloat(params, "tp_intercept", 0.20),
		BetFraction:      getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *GodStrategy) Name() string { return "GodStrategy" }

type godSessionSummary struct {
	WindowStart time.Time
	InitialBtc  float64
	FinalBtc    float64
	Outcome     string
}

func (s *GodStrategy) OnSessionStart(metadata map[string]any) {
	s.volatilityBps = nil
	s.bought = false
	s.entrySide = ""
	s.entryMidpoint = 0.0
	s.tpTriggered = false

	// Compute volatility from prior sessions (same as VolatilityBargain)
	var allSummaries []godSessionSummary
	if prior, ok := metadata["_prior_session_summaries"]; ok {
		if priorList, ok := prior.([]map[string]any); ok {
			for _, m := range priorList {
				ws, _ := m["window_start"].(time.Time)
				ib, _ := m["initial_btc"].(float64)
				fb, _ := m["final_btc"].(float64)
				outcome, _ := m["outcome"].(string)
				allSummaries = append(allSummaries, godSessionSummary{
					WindowStart: ws,
					InitialBtc:  ib,
					FinalBtc:    fb,
					Outcome:     outcome,
				})
			}
		}
	}

	sort.Slice(allSummaries, func(i, j int) bool {
		return allSummaries[i].WindowStart.Before(allSummaries[j].WindowStart)
	})

	var valid []godSessionSummary
	for _, ss := range allSummaries {
		if ss.InitialBtc > 0 && ss.FinalBtc > 0 {
			valid = append(valid, ss)
		}
	}

	if len(valid) > s.LookbackSessions {
		valid = valid[len(valid)-s.LookbackSessions:]
	}

	if len(valid) < s.MinSessions {
		return
	}

	var returnsBps []float64
	for _, ss := range valid {
		ret := (ss.FinalBtc - ss.InitialBtc) / ss.InitialBtc * 10000.0
		returnsBps = append(returnsBps, ret)
	}

	meanRet := 0.0
	for _, r := range returnsBps {
		meanRet += r
	}
	meanRet /= float64(len(returnsBps))

	variance := 0.0
	for _, r := range returnsBps {
		variance += (r - meanRet) * (r - meanRet)
	}
	variance /= float64(len(returnsBps))
	vol := math.Sqrt(variance)

	if vol <= 0 {
		return
	}
	s.volatilityBps = &vol
}

func (s *GodStrategy) heldSide(ctx core.TickContext) string {
	if ctx.Position.UpTokens > 0 {
		return "up"
	}
	if ctx.Position.DownTokens > 0 {
		return "down"
	}
	return ""
}

func (s *GodStrategy) OnTick(ctx core.TickContext) core.TradeAction {
	upMid, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downMid, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	if !okU || !okD {
		return core.Hold
	}

	// Manage existing position -- take-profit
	held := s.heldSide(ctx)
	if held != "" && !s.tpTriggered {
		currentMid := upMid
		if held == "down" {
			currentMid = downMid
		}
		if s.entryMidpoint > 0 && currentMid > 0 && currentMid < 0.99 {
			tpTarget := s.TpSlope*ctx.ElapsedFraction + s.TpIntercept
			if currentMid >= s.entryMidpoint+tpTarget {
				sellSide := "sell_up"
				tokens := ctx.Position.UpTokens
				if held == "down" {
					sellSide = "sell_down"
					tokens = ctx.Position.DownTokens
				}
				s.tpTriggered = true
				s.Log(fmt.Sprintf("TAKE PROFIT: mid=%.3f target=%.3f buy=%.3f",
					currentMid, tpTarget, s.entryMidpoint))
				return core.TradeAction{Side: sellSide, Amount: tokens, Price: &currentMid}
			}
		}
		return core.Hold
	}

	if s.volatilityBps == nil || s.bought {
		return core.Hold
	}

	if ctx.ElapsedFraction < s.MinElapsed {
		return core.Hold
	}

	btcNow, okB := sampleFloat(ctx.Sample, "btc_price_usd")
	if !okB || ctx.InitialBtcPrice <= 0 {
		return core.Hold
	}

	btcChangeBps := ((btcNow - ctx.InitialBtcPrice) / ctx.InitialBtcPrice) * 10000.0
	absChange := math.Abs(btcChangeBps)

	// Require BTC to have moved within range
	if absChange < s.MinBtcMoveBps || absChange > s.MaxBtcMoveBps {
		return core.Hold
	}

	// Contrarian: buy opposite of BTC direction
	targetSide := "down"
	if btcChangeBps < 0 {
		targetSide = "up"
	}

	mid := upMid
	if targetSide == "down" {
		mid = downMid
	}

	if mid <= 0 || mid > s.MaxTokenPrice {
		return core.Hold
	}

	// Score = volatility * sqrt(1 - elapsed) / midpoint
	remaining := 1.0 - ctx.ElapsedFraction
	if remaining < 0.001 {
		remaining = 0.001
	}
	timeFactor := math.Sqrt(remaining)
	score := *s.volatilityBps * timeFactor / mid

	if score <= s.Threshold {
		return core.Hold
	}

	s.bought = true
	s.entrySide = targetSide
	s.entryMidpoint = mid

	buySide := "buy_up"
	if targetSide == "down" {
		buySide = "buy_down"
	}
	s.Log(fmt.Sprintf("BUY %s: score=%.1f mid=%.3f vol=%.2f elapsed=%.2f btc_bps=%.1f",
		buySide, score, mid, *s.volatilityBps, ctx.ElapsedFraction, btcChangeBps))

	return core.TradeAction{
		Side:   buySide,
		Amount: ctx.Position.Cash * s.BetFraction,
		Price:  &mid,
	}
}

func (s *GodStrategy) ParamGrid() map[string][]any {
	return map[string][]any{
		"threshold":        {20.0, 35.0, 50.0, 70.0, 100.0},
		"max_token_price":  {0.08, 0.10, 0.12, 0.15, 0.20},
		"min_btc_move_bps": {1.0, 2.0, 3.0, 5.0},
		"max_btc_move_bps": {10.0, 15.0, 20.0},
		"tp_intercept":     {0.2, 0.3, 0.4},
	}
}
