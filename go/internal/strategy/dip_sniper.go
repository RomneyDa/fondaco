package strategy

import (
	"fmt"
	"math"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// DipSniper is a high-conviction directional entry strategy with limit
// orders and confirmation-based loss cutting.
//
// Combines midpoint level, BTC momentum, and midpoint trend to predict
// the winning side. Buys via limit order at a discount. After fill,
// applies hard stop-loss always and soft confirmation check after
// ConfirmSecs.
type DipSniper struct {
	BaseStrategy
	SignalFrac     float64
	MinMidpoint    float64
	MinBtcBps      float64
	LimitOffset    float64
	OrderExpiryFrac float64
	ConfirmSecs    float64
	SoftThreshold  float64
	HardSL         float64
	BetFraction    float64

	ordered        bool
	filled         bool
	fillMidpoint   float64
	fillSide       string
	confirmed      bool
	windowSeconds  float64
}

// NewDipSniper creates a DipSniper from params.
func NewDipSniper(params map[string]any) Strategy {
	return &DipSniper{
		SignalFrac:      getFloat(params, "signal_frac", 0.25),
		MinMidpoint:     getFloat(params, "min_midpoint", 0.55),
		MinBtcBps:       getFloat(params, "min_btc_bps", 0.0),
		LimitOffset:     getFloat(params, "limit_offset", 0.02),
		OrderExpiryFrac: getFloat(params, "order_expiry_frac", 0.50),
		ConfirmSecs:     getFloat(params, "confirm_secs", 30.0),
		SoftThreshold:   getFloat(params, "soft_threshold", 0.02),
		HardSL:          getFloat(params, "hard_sl", 0.12),
		BetFraction:     getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *DipSniper) Name() string { return "DipSniper" }

func (s *DipSniper) OnSessionStart(metadata map[string]any) {
	s.ordered = false
	s.filled = false
	s.fillMidpoint = 0.0
	s.fillSide = ""
	s.confirmed = false
	s.windowSeconds = getFloat(metadata, "window", 300)
}

// pickDirection returns "up" or "down" if signals converge, else "".
func (s *DipSniper) pickDirection(ctx core.TickContext) string {
	upMid, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downMid, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	btcPrice, okB := sampleFloat(ctx.Sample, "btc_price_usd")

	if !okU || !okD || !okB {
		return ""
	}

	// Which side has higher midpoint = favored
	favored := "down"
	favoredMid := downMid
	if upMid > downMid {
		favored = "up"
		favoredMid = upMid
	}

	// Minimum midpoint check
	if favoredMid < s.MinMidpoint {
		return ""
	}

	// BTC momentum check
	if ctx.InitialBtcPrice > 0 && s.MinBtcBps > 0 {
		btcBps := ((btcPrice - ctx.InitialBtcPrice) / ctx.InitialBtcPrice) * 10000
		// BTC direction should agree with favored side
		if favored == "up" && btcBps < s.MinBtcBps {
			return ""
		}
		if favored == "down" && btcBps > -s.MinBtcBps {
			return ""
		}
	}

	return favored
}

func (s *DipSniper) OnTick(ctx core.TickContext) core.TradeAction {
	// Check for fills from open orders
	if s.ordered && !s.filled {
		// Detect fill: we have tokens now
		if ctx.Position.UpTokens > 0 {
			s.filled = true
			s.fillSide = "up"
			mid, _ := sampleFloat(ctx.Sample, "up_token_midpoint")
			s.fillMidpoint = mid
			elapsedSecs, _ := sampleFloat(ctx.Sample, "elapsed_seconds")
			s.Log(fmt.Sprintf("FILLED at %.3f, elapsed=%.1fs", mid, elapsedSecs))
		} else if ctx.Position.DownTokens > 0 {
			s.filled = true
			s.fillSide = "down"
			mid, _ := sampleFloat(ctx.Sample, "down_token_midpoint")
			s.fillMidpoint = mid
			elapsedSecs, _ := sampleFloat(ctx.Sample, "elapsed_seconds")
			s.Log(fmt.Sprintf("FILLED at %.3f, elapsed=%.1fs", mid, elapsedSecs))
		} else if len(ctx.OpenOrders) == 0 {
			// Order expired without fill
			elapsedSecs, _ := sampleFloat(ctx.Sample, "elapsed_seconds")
			s.Log(fmt.Sprintf("ORDER EXPIRED: %s at %.3f, elapsed_frac=%.2f",
				s.fillSide, s.fillMidpoint, ctx.ElapsedFraction))
			_ = elapsedSecs
			s.ordered = false
			// Allow re-evaluation
		}
	}

	// Manage filled position
	if s.filled {
		midKey := "up_token_midpoint"
		if s.fillSide == "down" {
			midKey = "down_token_midpoint"
		}
		mid, ok := sampleFloat(ctx.Sample, midKey)
		if !ok {
			return core.Hold
		}

		elapsedSecs, _ := sampleFloat(ctx.Sample, "elapsed_seconds")

		tokens := ctx.Position.UpTokens
		sellSide := "sell_up"
		if s.fillSide == "down" {
			tokens = ctx.Position.DownTokens
			sellSide = "sell_down"
		}

		if tokens <= 0 {
			return core.Hold
		}

		// Hard stop-loss: always active
		if s.fillMidpoint > 0 && mid <= s.fillMidpoint-s.HardSL {
			s.Log(fmt.Sprintf("STOP LOSS: mid=%.3f buy=%.3f drop=%.3f",
				mid, s.fillMidpoint, s.fillMidpoint-mid))
			return core.TradeAction{Side: sellSide, Amount: tokens}
		}

		// Soft confirmation check after ConfirmSecs
		if !s.confirmed && elapsedSecs > 0 {
			// Check how long since fill -- approximate
			if elapsedSecs >= s.ConfirmSecs {
				if mid < s.fillMidpoint-s.SoftThreshold {
					s.Log(fmt.Sprintf("CUT LOSS: mid=%.3f buy=%.3f after %.0fs",
						mid, s.fillMidpoint, elapsedSecs))
					return core.TradeAction{Side: sellSide, Amount: tokens}
				}
				s.confirmed = true
				s.Log(fmt.Sprintf("CONFIRMED: mid=%.3f buy=%.3f", mid, s.fillMidpoint))
			}
		}

		return core.Hold
	}

	// Entry logic: place limit order
	if s.ordered {
		return core.Hold
	}

	// Only enter during signal window
	if ctx.ElapsedFraction > s.SignalFrac {
		return core.Hold
	}

	direction := s.pickDirection(ctx)
	if direction == "" {
		return core.Hold
	}

	midKey := "up_token_midpoint"
	if direction == "down" {
		midKey = "down_token_midpoint"
	}
	currentMid, ok := sampleFloat(ctx.Sample, midKey)
	if !ok {
		return core.Hold
	}

	// Limit price at discount
	limitPrice := currentMid - s.LimitOffset

	// Clamp to valid range
	limitPrice = math.Round(limitPrice*100) / 100
	if limitPrice < 0.05 {
		limitPrice = 0.05
	}
	if limitPrice > 0.95 {
		limitPrice = 0.95
	}

	// Compute expiration
	expirationSecs := s.OrderExpiryFrac * s.windowSeconds

	s.ordered = true
	s.fillSide = direction // store for logging on expiry

	buySide := "buy_up"
	if direction == "down" {
		buySide = "buy_down"
	}
	dollars := ctx.Position.Cash * s.BetFraction

	s.Log(fmt.Sprintf("ORDER: %s at %.3f (mid=%.3f offset=%.3f) exp=%.0fs",
		buySide, limitPrice, currentMid, s.LimitOffset, expirationSecs))

	return core.TradeAction{
		Side:              buySide,
		Amount:            dollars,
		Price:             &limitPrice,
		ExpirationSeconds: &expirationSecs,
	}
}

func (s *DipSniper) ParamGrid() map[string][]any {
	return map[string][]any{
		"signal_frac":      {0.2, 0.25, 0.3},
		"min_midpoint":     {0.52, 0.55, 0.60},
		"min_btc_bps":      {0.0, 5.0},
		"limit_offset":     {0.01, 0.02, 0.03},
		"order_expiry_frac": {0.35, 0.50},
		"confirm_secs":     {15.0, 30.0},
		"soft_threshold":   {0.02},
		"hard_sl":          {0.08, 0.12, 0.15},
	}
}
