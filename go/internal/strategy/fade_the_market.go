package strategy

import (
	"fmt"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// FadeTheMarket is a mean-reversion contrarian strategy. If a token midpoint
// stays at an extreme for several ticks early in the window, bets against it.
type FadeTheMarket struct {
	BaseStrategy
	OverreactionThreshold float64
	PersistTicks          int
	MaxElapsed            float64
	StopLossMid           float64
	BetFraction           float64

	fadedSide   string
	exited      bool
	streakSide  string
	streakCount int
}

// NewFadeTheMarket creates a FadeTheMarket from params.
func NewFadeTheMarket(params map[string]any) Strategy {
	return &FadeTheMarket{
		OverreactionThreshold: getFloat(params, "overreaction_threshold", 0.75),
		PersistTicks:          getInt(params, "persist_ticks", 5),
		MaxElapsed:            getFloat(params, "max_elapsed", 0.3),
		StopLossMid:           getFloat(params, "stop_loss_mid", 0.85),
		BetFraction:           getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *FadeTheMarket) Name() string { return "FadeTheMarket" }

func (s *FadeTheMarket) OnSessionStart(metadata map[string]any) {
	s.fadedSide = ""
	s.exited = false
	s.streakSide = ""
	s.streakCount = 0
}

func (s *FadeTheMarket) heldSide(ctx core.TickContext) string {
	if ctx.Position.UpTokens > 0 {
		return "up"
	}
	if ctx.Position.DownTokens > 0 {
		return "down"
	}
	return ""
}

func (s *FadeTheMarket) OnTick(ctx core.TickContext) core.TradeAction {
	upMid, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downMid, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	if !okU || !okD {
		return core.Hold
	}

	// Already entered -- manage position or wait for fill
	if s.fadedSide != "" {
		held := s.heldSide(ctx)
		if held != "" {
			// Check stop-loss
			fadedMid := upMid
			if s.fadedSide == "down" {
				fadedMid = downMid
			}
			if fadedMid >= s.StopLossMid {
				currentMid := upMid
				if held == "down" {
					currentMid = downMid
				}
				s.Log(fmt.Sprintf("stop-loss: faded %s mid=%.2f >= %.2f",
					s.fadedSide, fadedMid, s.StopLossMid))
				sellSide := "sell_up"
				tokens := ctx.Position.UpTokens
				if held == "down" {
					sellSide = "sell_down"
					tokens = ctx.Position.DownTokens
				}
				s.fadedSide = ""
				s.exited = true
				return core.TradeAction{Side: sellSide, Amount: tokens, Price: &currentMid}
			}
		}
		return core.Hold
	}

	// Already filled and exited (stop-loss)
	if s.exited {
		return core.Hold
	}

	if ctx.ElapsedFraction > s.MaxElapsed {
		return core.Hold
	}

	// Track persistence -- consecutive ticks above threshold
	if upMid >= s.OverreactionThreshold {
		if s.streakSide == "up" {
			s.streakCount++
		} else {
			s.streakSide = "up"
			s.streakCount = 1
		}
	} else if downMid >= s.OverreactionThreshold {
		if s.streakSide == "down" {
			s.streakCount++
		} else {
			s.streakSide = "down"
			s.streakCount = 1
		}
	} else {
		s.streakSide = ""
		s.streakCount = 0
		return core.Hold
	}

	if s.streakCount < s.PersistTicks {
		return core.Hold
	}

	// Persistent overreaction confirmed -- fade it
	dollars := ctx.Position.Cash * s.BetFraction
	if s.streakSide == "up" {
		s.fadedSide = "up"
		s.Log(fmt.Sprintf("fading up mid=%.2f (%d ticks), buying down mid=%.2f",
			upMid, s.streakCount, downMid))
		return core.TradeAction{Side: "buy_down", Amount: dollars, Price: &downMid}
	} else if s.streakSide == "down" {
		s.fadedSide = "down"
		s.Log(fmt.Sprintf("fading down mid=%.2f (%d ticks), buying up mid=%.2f",
			downMid, s.streakCount, upMid))
		return core.TradeAction{Side: "buy_up", Amount: dollars, Price: &upMid}
	}

	return core.Hold
}

func (s *FadeTheMarket) ParamGrid() map[string][]any {
	return map[string][]any{
		"overreaction_threshold": {0.70, 0.75, 0.78, 0.80},
		"persist_ticks":          {3, 5, 8},
		"max_elapsed":            {0.2, 0.3, 0.4},
		"stop_loss_mid":          {0.82, 0.85, 0.90},
		"bet_fraction":           {0.25, 0.5, 1.0},
	}
}
