package strategy

import (
	"fmt"
	"math"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// normCDF computes the standard normal CDF via math.Erf.
func normCDF(x float64) float64 {
	return 0.5 * (1.0 + math.Erf(x/math.Sqrt(2.0)))
}

// DigitalOption models the market as a digital option using Black-Scholes
// fair probability. Enters when market midpoint diverges from model price,
// with regime-adaptive thresholds.
type DigitalOption struct {
	BaseStrategy
	SigmaSpan      int
	MomentumSpan   int
	MomentumWeight float64
	BaseEdge       float64
	NoNewAfter     float64
	StopLossPct    float64
	BetFraction    float64

	prevBtc       float64
	sigmaSq       float64
	momentumEwma  float64
	warmupReturns []float64
	warmupDone    bool
	entryMidpoint float64
	entrySide     string
	exited        bool
}

// NewDigitalOption creates a DigitalOption from params.
func NewDigitalOption(params map[string]any) Strategy {
	return &DigitalOption{
		SigmaSpan:      getInt(params, "sigma_span", 15),
		MomentumSpan:   getInt(params, "momentum_span", 32),
		MomentumWeight: getFloat(params, "momentum_weight", 0.05),
		BaseEdge:       getFloat(params, "base_edge", 0.25),
		NoNewAfter:     getFloat(params, "no_new_after", 0.70),
		StopLossPct:    getFloat(params, "stop_loss_pct", 0.06),
		BetFraction:    getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *DigitalOption) Name() string { return "DigitalOption" }

func (s *DigitalOption) OnSessionStart(metadata map[string]any) {
	s.prevBtc = 0.0
	s.sigmaSq = 0.0
	s.momentumEwma = 0.0
	s.warmupReturns = nil
	s.warmupDone = false
	s.entryMidpoint = 0.0
	s.entrySide = ""
	s.exited = false
}

func (s *DigitalOption) heldSide(ctx core.TickContext) string {
	if ctx.Position.UpTokens > 0 {
		return "up"
	}
	if ctx.Position.DownTokens > 0 {
		return "down"
	}
	return ""
}

func (s *DigitalOption) OnTick(ctx core.TickContext) core.TradeAction {
	btc, okB := sampleFloat(ctx.Sample, "btc_price_usd")
	upMid, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downMid, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	if !okB || !okU || !okD {
		return core.Hold
	}

	// Update EWMA statistics
	if s.prevBtc > 0 {
		logRet := math.Log(btc / s.prevBtc)
		warmupLen := s.SigmaSpan / 5
		if warmupLen < 10 {
			warmupLen = 10
		}

		if !s.warmupDone {
			s.warmupReturns = append(s.warmupReturns, logRet)
			if len(s.warmupReturns) >= warmupLen {
				// Seed sigma_sq with sample variance
				meanR := 0.0
				for _, r := range s.warmupReturns {
					meanR += r
				}
				meanR /= float64(len(s.warmupReturns))
				variance := 0.0
				for _, r := range s.warmupReturns {
					variance += (r - meanR) * (r - meanR)
				}
				variance /= float64(len(s.warmupReturns))
				s.sigmaSq = variance
				s.momentumEwma = meanR
				s.warmupDone = true
				s.warmupReturns = nil
			}
		} else {
			alphaS := 2.0 / (float64(s.SigmaSpan) + 1.0)
			s.sigmaSq = alphaS*(logRet*logRet) + (1.0-alphaS)*s.sigmaSq

			alphaM := 2.0 / (float64(s.MomentumSpan) + 1.0)
			s.momentumEwma = alphaM*logRet + (1.0-alphaM)*s.momentumEwma
		}
	}
	s.prevBtc = btc

	// Need warmup before computing model
	if !s.warmupDone {
		return core.Hold
	}

	// Manage existing position: stop-loss
	held := s.heldSide(ctx)
	if held != "" {
		currentMid := upMid
		if held == "down" {
			currentMid = downMid
		}
		if s.entryMidpoint > 0 && currentMid <= s.entryMidpoint*(1.0-s.StopLossPct) {
			s.Log(fmt.Sprintf("stop-loss: mid=%.3f down %.1f%% from entry %.3f",
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

	// Already entered or exited
	if s.exited || s.entrySide != "" {
		return core.Hold
	}

	// Late cutoff
	if ctx.ElapsedFraction >= s.NoNewAfter {
		return core.Hold
	}

	// Compute fair probability
	strike := ctx.InitialBtcPrice
	if strike <= 0 {
		return core.Hold
	}

	d := math.Log(btc / strike)
	sigmaTick := math.Sqrt(math.Max(s.sigmaSq, 1e-20))

	// Estimate ticks remaining
	elapsedSecs := ctx.ElapsedFraction * 300.0
	if elapsedSecs < 1.0 {
		return core.Hold
	}
	tickRate := float64(ctx.TickIndex+1) / elapsedSecs
	secsRemaining := (1.0 - ctx.ElapsedFraction) * 300.0
	n := math.Max(tickRate*secsRemaining, 1.0)

	denom := sigmaTick * math.Sqrt(n)
	if denom < 1e-12 {
		return core.Hold
	}

	// z without momentum (for regime detection)
	zRaw := d / denom

	// z with momentum tilt
	muAdj := s.MomentumWeight * s.momentumEwma * n
	z := (d + muAdj) / denom

	pFair := normCDF(z)

	// Regime detection
	absZRaw := math.Abs(zRaw)
	var regime string
	var threshold float64
	if absZRaw < 0.3 {
		regime = "near_strike"
		threshold = s.BaseEdge * 1.5
	} else if absZRaw < 1.0 {
		regime = "drifting"
		threshold = s.BaseEdge
	} else {
		regime = "decided"
		threshold = s.BaseEdge * 0.7
	}

	// Entry logic
	upEdge := pFair - upMid
	downEdge := (1.0 - pFair) - downMid

	var side string
	var midpoint, edge float64

	if upEdge > threshold && upEdge >= downEdge {
		side = "buy_up"
		midpoint = upMid
		edge = upEdge
	} else if downEdge > threshold {
		side = "buy_down"
		midpoint = downMid
		edge = downEdge
	} else {
		s.Log(fmt.Sprintf("HOLD %s: z=%.2f z_raw=%.2f p_fair=%.3f up_mid=%.3f down_mid=%.3f up_edge=%.3f down_edge=%.3f thresh=%.3f",
			regime, z, zRaw, pFair, upMid, downMid, upEdge, downEdge, threshold))
		return core.Hold
	}

	if side == "buy_up" {
		s.entrySide = "up"
	} else {
		s.entrySide = "down"
	}
	s.entryMidpoint = midpoint
	s.Log(fmt.Sprintf("ENTRY %s: %s edge=%.3f p_fair=%.3f mid=%.3f z=%.2f z_raw=%.2f sigma=%.6f at %.0f%%",
		regime, side, edge, pFair, midpoint, z, zRaw, sigmaTick, ctx.ElapsedFraction*100))

	return core.TradeAction{
		Side:   side,
		Amount: ctx.Position.Cash * s.BetFraction,
		Price:  &midpoint,
	}
}

func (s *DigitalOption) ParamGrid() map[string][]any {
	return map[string][]any{
		"sigma_span":      {8, 12, 15, 20, 30},
		"momentum_span":   {20, 28, 32, 40, 50},
		"momentum_weight": {0.0, 0.03, 0.05, 0.08, 0.15},
		"base_edge":       {0.18, 0.22, 0.25, 0.30, 0.38},
		"no_new_after":    {0.65, 0.70, 0.80},
		"stop_loss_pct":   {0.04, 0.06, 0.10},
	}
}
