package strategy

import (
	"fmt"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// MomentumEntry buys in the direction BTC is moving if it exceeds a
// threshold early in the window.
type MomentumEntry struct {
	BaseStrategy
	EntryWindow       float64
	PriceThresholdBps float64
	MidpointConfirm   float64
	StopLossPct       float64
	BetFraction       float64

	entered       bool
	entryMidpoint float64
	exited        bool
}

// NewMomentumEntry creates a MomentumEntry from params.
func NewMomentumEntry(params map[string]any) Strategy {
	return &MomentumEntry{
		EntryWindow:       getFloat(params, "entry_window", 0.2),
		PriceThresholdBps: getFloat(params, "price_threshold_bps", 35.0),
		MidpointConfirm:   getFloat(params, "midpoint_confirm", 0.52),
		StopLossPct:       getFloat(params, "stop_loss_pct", 0.15),
		BetFraction:       getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *MomentumEntry) Name() string { return "MomentumEntry" }

func (s *MomentumEntry) OnSessionStart(metadata map[string]any) {
	s.entered = false
	s.entryMidpoint = 0.0
	s.exited = false
}

func (s *MomentumEntry) heldSide(ctx core.TickContext) string {
	if ctx.Position.UpTokens > 0 {
		return "up"
	}
	if ctx.Position.DownTokens > 0 {
		return "down"
	}
	return ""
}

func (s *MomentumEntry) OnTick(ctx core.TickContext) core.TradeAction {
	upMid, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downMid, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	if !okU || !okD {
		return core.Hold
	}

	// Manage existing position -- check stop-loss
	held := s.heldSide(ctx)
	if held != "" {
		currentMid := upMid
		if held == "down" {
			currentMid = downMid
		}
		if s.entryMidpoint > 0 && currentMid <= s.entryMidpoint*(1.0-s.StopLossPct) {
			s.Log(fmt.Sprintf("stop-loss: mid=%.2f down %.1f%% from entry %.2f",
				currentMid, (1.0-currentMid/s.entryMidpoint)*100, s.entryMidpoint))
			sellSide := "sell_up"
			tokens := ctx.Position.UpTokens
			if held == "down" {
				sellSide = "sell_down"
				tokens = ctx.Position.DownTokens
			}
			s.exited = true
			return core.TradeAction{Side: sellSide, Amount: tokens, Price: &currentMid}
		}
		return core.Hold
	}

	// Already filled and exited (stop-loss) or already tried to enter
	if s.exited || s.entered {
		return core.Hold
	}
	if ctx.ElapsedFraction > s.EntryWindow {
		return core.Hold
	}

	btcNow, okBtc := sampleFloat(ctx.Sample, "btc_price_usd")
	if !okBtc || ctx.InitialBtcPrice <= 0 {
		return core.Hold
	}

	moveBps := ((btcNow - ctx.InitialBtcPrice) / ctx.InitialBtcPrice) * 10000.0

	if moveBps > s.PriceThresholdBps && upMid >= s.MidpointConfirm {
		s.entered = true
		s.entryMidpoint = upMid
		dollars := ctx.Position.Cash * s.BetFraction
		s.Log(fmt.Sprintf("BTC +%.1fbps, up mid=%.2f confirms, buying up", moveBps, upMid))
		return core.TradeAction{Side: "buy_up", Amount: dollars, Price: &upMid}
	} else if moveBps < -s.PriceThresholdBps && downMid >= s.MidpointConfirm {
		s.entered = true
		s.entryMidpoint = downMid
		dollars := ctx.Position.Cash * s.BetFraction
		s.Log(fmt.Sprintf("BTC %.1fbps, down mid=%.2f confirms, buying down", moveBps, downMid))
		return core.TradeAction{Side: "buy_down", Amount: dollars, Price: &downMid}
	}

	return core.Hold
}

func (s *MomentumEntry) ParamGrid() map[string][]any {
	return map[string][]any{
		"entry_window":        {0.1, 0.15, 0.2, 0.3, 0.4, 0.5},
		"price_threshold_bps": {15.0, 20.0, 25.0, 30.0, 35.0, 40.0},
		"midpoint_confirm":    {0.51, 0.52, 0.54},
		"stop_loss_pct":       {0.10, 0.15, 0.20},
		"bet_fraction":        {0.3, 0.5, 1.0},
	}
}
