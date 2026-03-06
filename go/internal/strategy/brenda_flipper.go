package strategy

import (
	"fmt"
	"math"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// BrendaFlipper buys a cheap token early, holds briefly, then flips on
// a gain target or cuts on a stop-loss. A BTC gap override lets big
// movers ride.
type BrendaFlipper struct {
	BaseStrategy
	EntryWindow    float64
	MinPrice       float64
	MaxPrice       float64
	HoldSeconds    float64
	DropPct        float64
	GainMult       float64
	BtcGapOverride float64
	BetFraction    float64

	entered       bool
	exited        bool
	entryMidpoint float64
	entrySide     string
	entryTick     int
	windowSeconds float64
}

// NewBrendaFlipper creates a BrendaFlipper from params.
func NewBrendaFlipper(params map[string]any) Strategy {
	return &BrendaFlipper{
		EntryWindow:    getFloat(params, "entry_window", 0.25),
		MinPrice:       getFloat(params, "min_price", 0.10),
		MaxPrice:       getFloat(params, "max_price", 0.29),
		HoldSeconds:    getFloat(params, "hold_seconds", 10.0),
		DropPct:        getFloat(params, "drop_pct", 0.15),
		GainMult:       getFloat(params, "gain_mult", 3.0),
		BtcGapOverride: getFloat(params, "btc_gap_override", 50.0),
		BetFraction:    getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *BrendaFlipper) Name() string { return "BrendaFlipper" }

func (s *BrendaFlipper) OnSessionStart(metadata map[string]any) {
	s.entered = false
	s.exited = false
	s.entryMidpoint = 0.0
	s.entrySide = ""
	s.entryTick = 0
	s.windowSeconds = getFloat(metadata, "window", 300)
}

func (s *BrendaFlipper) heldSide(ctx core.TickContext) string {
	if ctx.Position.UpTokens > 0 {
		return "up"
	}
	if ctx.Position.DownTokens > 0 {
		return "down"
	}
	return ""
}

func (s *BrendaFlipper) elapsedSeconds(ctx core.TickContext) float64 {
	return ctx.ElapsedFraction * s.windowSeconds
}

func (s *BrendaFlipper) OnTick(ctx core.TickContext) core.TradeAction {
	btc, okB := sampleFloat(ctx.Sample, "btc_price_usd")
	upMid, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downMid, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	if !okB || !okU || !okD {
		return core.Hold
	}

	// Manage existing position
	held := s.heldSide(ctx)
	if held != "" {
		if s.exited {
			return core.Hold
		}

		currentMid := upMid
		if held == "down" {
			currentMid = downMid
		}
		sellSide := "sell_up"
		tokens := ctx.Position.UpTokens
		if held == "down" {
			sellSide = "sell_down"
			tokens = ctx.Position.DownTokens
		}

		// Enforce minimum hold period
		elapsedSec := s.elapsedSeconds(ctx)
		tickIdx := ctx.TickIndex
		if tickIdx == 0 {
			tickIdx = 1
		}
		entrySec := (float64(s.entryTick) / float64(tickIdx)) * elapsedSec
		heldSeconds := elapsedSec - entrySec
		if heldSeconds < s.HoldSeconds {
			return core.Hold
		}

		// Stop-loss: token dropped >= drop_pct from entry
		if s.entryMidpoint > 0 && currentMid <= s.entryMidpoint*(1.0-s.DropPct) {
			s.Log(fmt.Sprintf("stop-loss: mid=%.3f down %.1f%% from entry %.3f",
				currentMid, (1.0-currentMid/s.entryMidpoint)*100, s.entryMidpoint))
			s.exited = true
			return core.TradeAction{Side: sellSide, Amount: tokens, Price: &currentMid}
		}

		// Take-profit: token reached gain_mult of entry
		if s.entryMidpoint > 0 && currentMid >= s.entryMidpoint*s.GainMult {
			// BTC gap override: if BTC moved far enough, let it ride
			btcGap := math.Abs(btc - ctx.InitialBtcPrice)
			if btcGap >= s.BtcGapOverride {
				s.Log(fmt.Sprintf("gain target hit but BTC gap $%.0f >= $%.0f, holding",
					btcGap, s.BtcGapOverride))
				return core.Hold
			}

			s.Log(fmt.Sprintf("take-profit: mid=%.3f up %.1fx from entry %.3f",
				currentMid, currentMid/s.entryMidpoint, s.entryMidpoint))
			s.exited = true
			return core.TradeAction{Side: sellSide, Amount: tokens, Price: &currentMid}
		}

		return core.Hold
	}

	// Already entered or exited
	if s.entered || s.exited {
		return core.Hold
	}

	// Entry logic: buy a cheap token in the first entry_window
	if ctx.ElapsedFraction >= s.EntryWindow {
		return core.Hold
	}

	// Find a token in the target price range
	type candidate struct {
		direction string
		midpoint  float64
	}
	var candidates []candidate
	if s.MinPrice <= upMid && upMid <= s.MaxPrice {
		candidates = append(candidates, candidate{"up", upMid})
	}
	if s.MinPrice <= downMid && downMid <= s.MaxPrice {
		candidates = append(candidates, candidate{"down", downMid})
	}

	if len(candidates) == 0 {
		return core.Hold
	}

	// Pick the cheaper token
	best := candidates[0]
	for _, c := range candidates[1:] {
		if c.midpoint < best.midpoint {
			best = c
		}
	}

	s.entered = true
	s.entrySide = best.direction
	s.entryMidpoint = best.midpoint
	s.entryTick = ctx.TickIndex

	side := "buy_up"
	if best.direction == "down" {
		side = "buy_down"
	}
	s.Log(fmt.Sprintf("ENTRY: %s mid=%.3f at %.0f%% elapsed",
		side, best.midpoint, ctx.ElapsedFraction*100))

	return core.TradeAction{
		Side:   side,
		Amount: ctx.Position.Cash * s.BetFraction,
		Price:  &best.midpoint,
	}
}

func (s *BrendaFlipper) ParamGrid() map[string][]any {
	return map[string][]any{
		"entry_window": {0.20, 0.22, 0.25, 0.28, 0.30},
		"max_price":    {0.25, 0.27, 0.29, 0.31, 0.33},
		"drop_pct":     {0.10, 0.12, 0.15, 0.18, 0.20},
		"gain_mult":    {2.0, 3.0, 5.0, 100.0},
		"hold_seconds": {5.0, 10.0, 15.0},
	}
}
