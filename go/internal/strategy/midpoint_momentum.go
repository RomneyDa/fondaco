package strategy

import (
	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// MidpointMomentum follows token midpoint trend over recent ticks.
// If a token's midpoint is rising with enough momentum above a threshold,
// buys that side.
type MidpointMomentum struct {
	BaseStrategy
	LookbackTicks     int
	MidpointThreshold float64
	MomentumThreshold float64
	EntryDeadline     float64
	BetFraction       float64

	entered bool
}

// NewMidpointMomentum creates a MidpointMomentum from params.
func NewMidpointMomentum(params map[string]any) Strategy {
	return &MidpointMomentum{
		LookbackTicks:     getInt(params, "lookback_ticks", 5),
		MidpointThreshold: getFloat(params, "midpoint_threshold", 0.55),
		MomentumThreshold: getFloat(params, "momentum_threshold", 0.03),
		EntryDeadline:     getFloat(params, "entry_deadline", 0.6),
		BetFraction:       getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *MidpointMomentum) Name() string { return "MidpointMomentum" }

func (s *MidpointMomentum) OnSessionStart(metadata map[string]any) {
	s.entered = false
}

func (s *MidpointMomentum) OnTick(ctx core.TickContext) core.TradeAction {
	if s.entered {
		return core.Hold
	}
	if ctx.ElapsedFraction > s.EntryDeadline {
		return core.Hold
	}
	if len(ctx.History) < s.LookbackTicks {
		return core.Hold
	}

	// Current midpoints
	upNow, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downNow, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	if !okU || !okD {
		return core.Hold
	}

	// Midpoint from lookback_ticks ago
	past := ctx.History[len(ctx.History)-s.LookbackTicks]
	upPast, okUP := sampleFloat(past, "up_token_midpoint")
	downPast, okDP := sampleFloat(past, "down_token_midpoint")
	if !okUP || !okDP {
		return core.Hold
	}

	upMomentum := upNow - upPast
	downMomentum := downNow - downPast

	if upNow >= s.MidpointThreshold && upMomentum >= s.MomentumThreshold {
		s.entered = true
		return core.TradeAction{Side: "buy_up", Amount: ctx.Position.Cash * s.BetFraction, Price: &upNow}
	} else if downNow >= s.MidpointThreshold && downMomentum >= s.MomentumThreshold {
		s.entered = true
		return core.TradeAction{Side: "buy_down", Amount: ctx.Position.Cash * s.BetFraction, Price: &downNow}
	}

	return core.Hold
}

func (s *MidpointMomentum) ParamGrid() map[string][]any {
	return map[string][]any{
		"lookback_ticks":     {3, 5, 10},
		"midpoint_threshold": {0.52, 0.55, 0.60},
		"momentum_threshold": {0.02, 0.03, 0.05},
		"entry_deadline":     {0.4, 0.6, 0.8},
		"bet_fraction":       {0.25, 0.5, 1.0},
	}
}
