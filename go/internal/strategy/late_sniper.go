package strategy

import (
	"fmt"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// LateSniper waits until late in the window when outcome is more certain,
// then buys the likely winner. Exits on stop-loss if the market reverses.
type LateSniper struct {
	BaseStrategy
	MinElapsed          float64
	ConfidenceThreshold float64
	MaxPrice            float64
	StopLossPct         float64
	BetFraction         float64

	entryMidpoint float64
	exited        bool
}

// NewLateSniper creates a LateSniper from params.
func NewLateSniper(params map[string]any) Strategy {
	return &LateSniper{
		MinElapsed:          getFloat(params, "min_elapsed", 0.53),
		ConfidenceThreshold: getFloat(params, "confidence_threshold", 0.60),
		MaxPrice:            getFloat(params, "max_price", 0.92),
		StopLossPct:         getFloat(params, "stop_loss_pct", 0.10),
		BetFraction:         getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *LateSniper) Name() string { return "LateSniper" }

func (s *LateSniper) OnSessionStart(metadata map[string]any) {
	s.entryMidpoint = 0.0
	s.exited = false
}

func (s *LateSniper) heldSide(ctx core.TickContext) string {
	if ctx.Position.UpTokens > 0 {
		return "up"
	}
	if ctx.Position.DownTokens > 0 {
		return "down"
	}
	return ""
}

func (s *LateSniper) OnTick(ctx core.TickContext) core.TradeAction {
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

	// Already entered (limit order pending or filled+exited)
	if s.exited || s.entryMidpoint > 0 {
		return core.Hold
	}

	// Wait for late window
	if ctx.ElapsedFraction < s.MinElapsed {
		return core.Hold
	}

	if upMid >= s.ConfidenceThreshold && upMid <= s.MaxPrice {
		s.entryMidpoint = upMid
		s.Log(fmt.Sprintf("BUY UP: mid=%.2f at %.0f%% elapsed", upMid, ctx.ElapsedFraction*100))
		return core.TradeAction{Side: "buy_up", Amount: ctx.Position.Cash * s.BetFraction, Price: &upMid}
	} else if downMid >= s.ConfidenceThreshold && downMid <= s.MaxPrice {
		s.entryMidpoint = downMid
		s.Log(fmt.Sprintf("BUY DOWN: mid=%.2f at %.0f%% elapsed", downMid, ctx.ElapsedFraction*100))
		return core.TradeAction{Side: "buy_down", Amount: ctx.Position.Cash * s.BetFraction, Price: &downMid}
	}

	// Log why we held
	bestMid := upMid
	bestSide := "up"
	if downMid > upMid {
		bestMid = downMid
		bestSide = "down"
	}
	reasons := ""
	if bestMid < s.ConfidenceThreshold {
		reasons = fmt.Sprintf("low confidence %.2f<%.2f", bestMid, s.ConfidenceThreshold)
	}
	if bestMid > s.MaxPrice {
		if reasons != "" {
			reasons += ", "
		}
		reasons += fmt.Sprintf("mid too high %.2f>%.2f", bestMid, s.MaxPrice)
	}
	if reasons == "" {
		reasons = "waiting"
	}
	s.Log(fmt.Sprintf("HOLD at %.0f%%: best=%s mid=%.2f | %s",
		ctx.ElapsedFraction*100, bestSide, bestMid, reasons))

	return core.Hold
}

func (s *LateSniper) ParamGrid() map[string][]any {
	return map[string][]any{
		"min_elapsed":          {0.45, 0.50, 0.53, 0.56, 0.60},
		"confidence_threshold": {0.55, 0.58, 0.60, 0.62, 0.65},
		"max_price":            {0.85, 0.89, 0.92, 0.95},
		"stop_loss_pct":        {0.05, 0.10, 0.15},
	}
}
