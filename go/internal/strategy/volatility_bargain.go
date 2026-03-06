package strategy

import (
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// VolatilityBargain buys cheap tokens when high recent BTC volatility
// makes them underpriced relative to remaining session time.
//
// Score = volatility_bps / (token_midpoint * elapsed_fraction).
// Never sells -- holds to resolution for the $1 payout.
type VolatilityBargain struct {
	BaseStrategy
	LookbackSessions int
	MinSessions      int
	Threshold        float64
	MaxTokenPrice    float64
	MinElapsed       float64
	SlopeLookback    float64
	SlopeThreshold   float64
	BetFraction      float64

	volatilityBps *float64
	bought        bool
}

// NewVolatilityBargain creates a VolatilityBargain from params.
func NewVolatilityBargain(params map[string]any) Strategy {
	return &VolatilityBargain{
		LookbackSessions: getInt(params, "lookback_sessions", 5),
		MinSessions:      getInt(params, "min_sessions", 5),
		Threshold:        getFloat(params, "threshold", 100.0),
		MaxTokenPrice:    getFloat(params, "max_token_price", 0.36),
		MinElapsed:       getFloat(params, "min_elapsed", 0.03),
		SlopeLookback:    getFloat(params, "slope_lookback", 4.5),
		SlopeThreshold:   getFloat(params, "slope_threshold", 0.0),
		BetFraction:      getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *VolatilityBargain) Name() string { return "VolatilityBargain" }

type volSessionSummary struct {
	WindowStart time.Time
	InitialBtc  float64
	FinalBtc    float64
	Outcome     string
}

func (s *VolatilityBargain) OnSessionStart(metadata map[string]any) {
	s.volatilityBps = nil
	s.bought = false

	// Get prior session summaries
	var allSummaries []volSessionSummary
	if prior, ok := metadata["_prior_session_summaries"]; ok {
		if priorList, ok := prior.([]map[string]any); ok {
			for _, m := range priorList {
				ws, _ := m["window_start"].(time.Time)
				ib, _ := m["initial_btc"].(float64)
				fb, _ := m["final_btc"].(float64)
				outcome, _ := m["outcome"].(string)
				allSummaries = append(allSummaries, volSessionSummary{
					WindowStart: ws,
					InitialBtc:  ib,
					FinalBtc:    fb,
					Outcome:     outcome,
				})
			}
		}
	}

	// Sort by time
	sort.Slice(allSummaries, func(i, j int) bool {
		return allSummaries[i].WindowStart.Before(allSummaries[j].WindowStart)
	})

	// Filter to valid sessions
	var valid []volSessionSummary
	for _, ss := range allSummaries {
		if ss.InitialBtc > 0 && ss.FinalBtc > 0 {
			valid = append(valid, ss)
		}
	}

	// Take the last lookback_sessions
	if len(valid) > s.LookbackSessions {
		valid = valid[len(valid)-s.LookbackSessions:]
	}

	if len(valid) < s.MinSessions {
		s.Log(fmt.Sprintf("Only %d sessions available (need %d)", len(valid), s.MinSessions))
		return
	}

	// Compute volatility: std dev of session BTC returns in bps
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
		s.Log("Zero volatility, skipping")
		return
	}
	s.volatilityBps = &vol
	s.Log(fmt.Sprintf("Volatility: %.2f bps from %d sessions", vol, len(valid)))
}

func (s *VolatilityBargain) OnTick(ctx core.TickContext) core.TradeAction {
	if s.volatilityBps == nil || s.bought {
		return core.Hold
	}

	if ctx.ElapsedFraction < s.MinElapsed {
		return core.Hold
	}

	upMid, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downMid, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	if !okU || !okD {
		return core.Hold
	}

	elapsed := ctx.ElapsedFraction
	if elapsed < 0.01 {
		elapsed = 0.01
	}

	vol := *s.volatilityBps

	// Score each token
	upScore := 0.0
	downScore := 0.0

	if upMid > 0 && upMid <= s.MaxTokenPrice {
		upScore = vol / (upMid * elapsed)
	}
	if downMid > 0 && downMid <= s.MaxTokenPrice {
		downScore = vol / (downMid * elapsed)
	}

	// Pick the higher-scoring token
	if upScore > s.Threshold && upScore >= downScore {
		s.bought = true
		s.Log(fmt.Sprintf("BUY UP: score=%.1f mid=%.3f vol=%.2f elapsed=%.2f",
			upScore, upMid, vol, ctx.ElapsedFraction))
		return core.TradeAction{
			Side:   "buy_up",
			Amount: ctx.Position.Cash * s.BetFraction,
			Price:  &upMid,
		}
	}

	if downScore > s.Threshold {
		s.bought = true
		s.Log(fmt.Sprintf("BUY DOWN: score=%.1f mid=%.3f vol=%.2f elapsed=%.2f",
			downScore, downMid, vol, ctx.ElapsedFraction))
		return core.TradeAction{
			Side:   "buy_down",
			Amount: ctx.Position.Cash * s.BetFraction,
			Price:  &downMid,
		}
	}

	return core.Hold
}

func (s *VolatilityBargain) ParamGrid() map[string][]any {
	return map[string][]any{
		"threshold":         {70.0, 85.0, 100.0, 120.0, 150.0, 180.0, 200.0},
		"max_token_price":   {0.22, 0.26, 0.30, 0.34, 0.38},
		"min_elapsed":       {0.03, 0.05, 0.08, 0.10},
		"lookback_sessions": {5, 8, 10},
	}
}
