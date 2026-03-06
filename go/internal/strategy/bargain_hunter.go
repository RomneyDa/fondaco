package strategy

import (
	"fmt"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// BargainHunter places one cheap GTD limit buy early in the session.
// If midpoint dips to order price, fills and holds to resolution.
//
// side_mode controls which token to buy:
//   - "cheaper": buy whichever token has the lower midpoint (contrarian)
//   - "pricier": buy whichever token has the higher midpoint (momentum)
//
// btc_cancel_bps: if BTC moves this many bps against the order direction,
// cancel the pending order to avoid a losing fill.
type BargainHunter struct {
	BaseStrategy
	BuyPrice       float64
	ExpirationFrac float64
	SideMode       string
	BtcCancelBps   *float64

	ordered       bool
	windowSeconds float64
}

// NewBargainHunter creates a BargainHunter from params.
func NewBargainHunter(params map[string]any) Strategy {
	return &BargainHunter{
		BuyPrice:       getFloat(params, "buy_price", 0.18),
		ExpirationFrac: getFloat(params, "expiration_frac", 0.34),
		SideMode:       getString(params, "side_mode", "pricier"),
		BtcCancelBps:   getFloatPtr(params, "btc_cancel_bps"),
	}
}

func (s *BargainHunter) Name() string { return "BargainHunter" }

func (s *BargainHunter) OnSessionStart(metadata map[string]any) {
	s.ordered = false
	s.windowSeconds = getFloat(metadata, "window", 300)
}

func (s *BargainHunter) OnTick(ctx core.TickContext) core.TradeAction {
	// Check open orders for BTC cancel guard
	if len(ctx.OpenOrders) > 0 && s.BtcCancelBps != nil {
		btcNow, okB := sampleFloat(ctx.Sample, "btc_price_usd")
		initial := ctx.InitialBtcPrice
		if okB && initial > 0 {
			moveBps := ((btcNow - initial) / initial) * 10000
			// If order is buy_up and BTC dropped, cancel
			// If order is buy_down and BTC rose, cancel
			for _, order := range ctx.OpenOrders {
				cancelSide := ""
				if order.Side == "buy_up" && moveBps < -*s.BtcCancelBps {
					cancelSide = "cancel_up"
				} else if order.Side == "buy_down" && moveBps > *s.BtcCancelBps {
					cancelSide = "cancel_down"
				}
				if cancelSide != "" {
					s.Log(fmt.Sprintf("BTC guard: cancel %s (%.1f bps > %.1f)",
						cancelSide, moveBps, *s.BtcCancelBps))
					return core.TradeAction{Side: cancelSide}
				}
			}
		}
	}

	// Already ordered or have open orders
	if s.ordered {
		return core.Hold
	}

	upMid, okU := sampleFloat(ctx.Sample, "up_token_midpoint")
	downMid, okD := sampleFloat(ctx.Sample, "down_token_midpoint")
	if !okU || !okD {
		return core.Hold
	}

	// Pick side
	var side string
	if s.SideMode == "cheaper" {
		if upMid <= downMid {
			side = "buy_up"
		} else {
			side = "buy_down"
		}
	} else {
		// "pricier" (default)
		if upMid >= downMid {
			side = "buy_up"
		} else {
			side = "buy_down"
		}
	}

	chosenMid := upMid
	if side == "buy_down" {
		chosenMid = downMid
	}

	s.ordered = true

	// Compute TTL in seconds
	ttlSeconds := s.ExpirationFrac * s.windowSeconds

	s.Log(fmt.Sprintf("GTD %s @ %.2f (mid=%.3f, ttl=%.0fs, mode=%s)",
		side, s.BuyPrice, chosenMid, ttlSeconds, s.SideMode))

	price := s.BuyPrice
	return core.TradeAction{
		Side:              side,
		Amount:            ctx.Position.Cash,
		Price:             &price,
		ExpirationSeconds: &ttlSeconds,
	}
}

func (s *BargainHunter) ParamGrid() map[string][]any {
	return map[string][]any{
		"buy_price":        {0.15, 0.16, 0.17, 0.18, 0.19, 0.20, 0.21, 0.22, 0.23, 0.24, 0.25},
		"expiration_frac":  {0.30, 0.32, 0.34, 0.36, 0.38, 0.40, 0.42, 0.44, 0.46, 0.48, 0.50},
		"side_mode":        {"cheaper", "pricier"},
		"btc_cancel_bps":   {5.0, 10.0, 15.0, 20.0},
	}
}
