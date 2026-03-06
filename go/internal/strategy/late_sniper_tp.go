package strategy

import (
	"fmt"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// LateSniperTP extends LateSniper with a take-profit exit.
// Sells early when ROI target is hit.
type LateSniperTP struct {
	BaseStrategy
	MinElapsed          float64
	ConfidenceThreshold float64
	MaxPrice            float64
	StopLossPct         float64
	TakeProfitPct       float64
	BetFraction         float64

	entryMidpoint float64
	exited        bool
}

// NewLateSniperTP creates a LateSniperTP from params.
func NewLateSniperTP(params map[string]any) Strategy {
	return &LateSniperTP{
		MinElapsed:          getFloat(params, "min_elapsed", 0.92),
		ConfidenceThreshold: getFloat(params, "confidence_threshold", 0.60),
		MaxPrice:            getFloat(params, "max_price", 0.90),
		StopLossPct:         getFloat(params, "stop_loss_pct", 0.20),
		TakeProfitPct:       getFloat(params, "take_profit_pct", 0.40),
		BetFraction:         getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *LateSniperTP) Name() string { return "LateSniperTP" }

func (s *LateSniperTP) OnSessionStart(metadata map[string]any) {
	s.entryMidpoint = 0.0
	s.exited = false
}

func (s *LateSniperTP) heldSide(ctx core.TickContext) string {
	if ctx.Position.UpTokens > 0 {
		return "up"
	}
	if ctx.Position.DownTokens > 0 {
		return "down"
	}
	return ""
}

func (s *LateSniperTP) OnTick(ctx core.TickContext) core.TradeAction {
	upMid, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downMid, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	if !okU || !okD {
		return core.Hold
	}

	// Manage existing position -- check stop-loss and take-profit
	held := s.heldSide(ctx)
	if held != "" {
		currentMid := upMid
		if held == "down" {
			currentMid = downMid
		}
		if s.entryMidpoint > 0 {
			sellSide := "sell_up"
			tokens := ctx.Position.UpTokens
			if held == "down" {
				sellSide = "sell_down"
				tokens = ctx.Position.DownTokens
			}

			// Stop-loss
			if currentMid <= s.entryMidpoint*(1.0-s.StopLossPct) {
				s.Log(fmt.Sprintf("stop-loss: mid=%.2f down %.1f%% from entry %.2f",
					currentMid, (1.0-currentMid/s.entryMidpoint)*100, s.entryMidpoint))
				s.exited = true
				return core.TradeAction{Side: sellSide, Amount: tokens, Price: &currentMid}
			}

			// Take-profit
			if currentMid >= s.entryMidpoint*(1.0+s.TakeProfitPct) {
				s.Log(fmt.Sprintf("take-profit: mid=%.2f up %.1f%% from entry %.2f",
					currentMid, (currentMid/s.entryMidpoint-1.0)*100, s.entryMidpoint))
				s.exited = true
				return core.TradeAction{Side: sellSide, Amount: tokens, Price: &currentMid}
			}
		}
		return core.Hold
	}

	// Already entered or exited
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

func (s *LateSniperTP) ParamGrid() map[string][]any {
	return map[string][]any{
		"min_elapsed":          {0.88, 0.90, 0.92, 0.94},
		"confidence_threshold": {0.55, 0.58, 0.60, 0.62, 0.65},
		"max_price":            {0.85, 0.88, 0.90, 0.93, 0.95},
		"stop_loss_pct":        {0.15, 0.18, 0.20, 0.22},
		"take_profit_pct":      {0.30, 0.35, 0.40, 0.50},
	}
}
