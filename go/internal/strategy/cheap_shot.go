package strategy

import (
	"fmt"
	"math"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// CheapShot buys cheap tokens early when BTC price is near the session
// opening price, using slight BTC momentum to pick the direction.
type CheapShot struct {
	BaseStrategy
	MaxDeviationBps  float64
	EntryWindow      float64
	MomentumLookback int
	MinMomentumBps   float64
	MaxTokenPrice    float64
	StopLossPct      float64
	StopLossAfter    float64
	TakeProfitPct    float64
	BetFraction      float64

	btcHistory    []float64
	entered       bool
	exited        bool
	entryMidpoint float64
	entrySide     string
}

// NewCheapShot creates a CheapShot from params.
func NewCheapShot(params map[string]any) Strategy {
	return &CheapShot{
		MaxDeviationBps:  getFloat(params, "max_deviation_bps", 3.0),
		EntryWindow:      getFloat(params, "entry_window", 0.25),
		MomentumLookback: getInt(params, "momentum_lookback", 50),
		MinMomentumBps:   getFloat(params, "min_momentum_bps", 0.0),
		MaxTokenPrice:    getFloat(params, "max_token_price", 0.60),
		StopLossPct:      getFloat(params, "stop_loss_pct", 0.15),
		StopLossAfter:    getFloat(params, "stop_loss_after", 0.5),
		TakeProfitPct:    getFloat(params, "take_profit_pct", 0.20),
		BetFraction:      getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *CheapShot) Name() string { return "CheapShot" }

func (s *CheapShot) OnSessionStart(metadata map[string]any) {
	s.btcHistory = nil
	s.entered = false
	s.exited = false
	s.entryMidpoint = 0.0
	s.entrySide = ""
}

func (s *CheapShot) heldSide(ctx core.TickContext) string {
	if ctx.Position.UpTokens > 0 {
		return "up"
	}
	if ctx.Position.DownTokens > 0 {
		return "down"
	}
	return ""
}

func (s *CheapShot) OnTick(ctx core.TickContext) core.TradeAction {
	btc, okB := sampleFloat(ctx.Sample, "btc_price_usd")
	upMid, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downMid, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	if !okB || !okU || !okD {
		return core.Hold
	}

	s.btcHistory = append(s.btcHistory, btc)

	// Manage existing position: take-profit / stop-loss
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

		// Take-profit: can trigger at any time
		if s.entryMidpoint > 0 && currentMid >= s.entryMidpoint*(1.0+s.TakeProfitPct) {
			s.Log(fmt.Sprintf("take-profit: mid=%.3f up %.1f%% from entry %.3f",
				currentMid, (currentMid/s.entryMidpoint-1.0)*100, s.entryMidpoint))
			s.exited = true
			return core.TradeAction{Side: sellSide, Amount: tokens, Price: &currentMid}
		}

		// Stop-loss: only after stop_loss_after fraction of window
		if ctx.ElapsedFraction >= s.StopLossAfter {
			if s.entryMidpoint > 0 && currentMid <= s.entryMidpoint*(1.0-s.StopLossPct) {
				s.Log(fmt.Sprintf("stop-loss: mid=%.3f down %.1f%% from entry %.3f",
					currentMid, (1.0-currentMid/s.entryMidpoint)*100, s.entryMidpoint))
				s.exited = true
				return core.TradeAction{Side: sellSide, Amount: tokens, Price: &currentMid}
			}
		}

		return core.Hold
	}

	// Already entered or exited
	if s.entered || s.exited {
		return core.Hold
	}

	// Only enter during early window
	if ctx.ElapsedFraction >= s.EntryWindow {
		return core.Hold
	}

	// Check BTC proximity to opening price
	openingBtc := ctx.InitialBtcPrice
	if openingBtc <= 0 {
		return core.Hold
	}

	deviationBps := math.Abs(btc-openingBtc) / openingBtc * 10000.0
	if deviationBps > s.MaxDeviationBps {
		return core.Hold
	}

	// Check token prices are cheap enough
	if upMid > s.MaxTokenPrice && downMid > s.MaxTokenPrice {
		return core.Hold
	}

	// Need enough history for momentum
	if len(s.btcHistory) < s.MomentumLookback {
		return core.Hold
	}

	// Compute momentum over lookback window (bps change)
	lookbackBtc := s.btcHistory[len(s.btcHistory)-s.MomentumLookback]
	momentumBps := (btc - lookbackBtc) / lookbackBtc * 10000.0

	// Pick direction based on momentum
	direction := ""
	if math.Abs(momentumBps) < s.MinMomentumBps {
		// Momentum too weak -- buy the cheaper token
		if upMid <= downMid && upMid <= s.MaxTokenPrice {
			direction = "up"
		} else if downMid <= s.MaxTokenPrice {
			direction = "down"
		} else {
			return core.Hold
		}
	} else if momentumBps > 0 {
		direction = "up"
	} else {
		direction = "down"
	}

	midpoint := upMid
	if direction == "down" {
		midpoint = downMid
	}
	if midpoint > s.MaxTokenPrice {
		// Favored side too expensive, try the other
		if direction == "up" {
			direction = "down"
		} else {
			direction = "up"
		}
		if direction == "up" {
			midpoint = upMid
		} else {
			midpoint = downMid
		}
		if midpoint > s.MaxTokenPrice {
			return core.Hold
		}
	}

	s.entered = true
	s.entrySide = direction
	s.entryMidpoint = midpoint

	side := "buy_up"
	if direction == "down" {
		side = "buy_down"
	}
	s.Log(fmt.Sprintf("ENTRY: %s mid=%.3f dev=%.1fbps mom=%.2fbps at %.0f%%",
		side, midpoint, deviationBps, momentumBps, ctx.ElapsedFraction*100))

	return core.TradeAction{
		Side:   side,
		Amount: ctx.Position.Cash * s.BetFraction,
		Price:  &midpoint,
	}
}

func (s *CheapShot) ParamGrid() map[string][]any {
	return map[string][]any{
		"max_deviation_bps": {3.0, 5.0, 8.0, 12.0},
		"entry_window":      {0.15, 0.25, 0.35},
		"momentum_lookback": {30, 50, 100},
		"max_token_price":   {0.52, 0.55, 0.60},
		"stop_loss_pct":     {0.15, 0.20, 0.30},
		"take_profit_pct":   {0.20, 0.30, 0.50},
	}
}
