// Package strategy defines the Strategy interface, a base helper, param
// extraction utilities, and the global strategy registries.
package strategy

import (
	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// Strategy is the interface all trading strategies must implement.
type Strategy interface {
	Name() string
	OnSessionStart(metadata map[string]any)
	OnTick(ctx core.TickContext) core.TradeAction
	ParamGrid() map[string][]any
	DrainTickLogs() []string
}

// StrategyFactory creates a new Strategy from a parameter map.
type StrategyFactory func(params map[string]any) Strategy

// UpdownStrategies contains all strategies for crypto up/down markets.
var UpdownStrategies map[string]StrategyFactory

// AllStrategies is the union of all strategy registries (backward compat).
var AllStrategies map[string]StrategyFactory

// marketStrategies maps market type to its strategy registry.
var marketStrategies map[string]map[string]StrategyFactory

// GetStrategiesForMarket returns the strategy registry for a given market type.
func GetStrategiesForMarket(marketType string) map[string]StrategyFactory {
	if reg, ok := marketStrategies[marketType]; ok {
		return reg
	}
	return nil
}

func init() {
	UpdownStrategies = map[string]StrategyFactory{
		"LateSniper":        NewLateSniper,
		"LateSniperTP":      NewLateSniperTP,
		"DigitalOption":     NewDigitalOption,
		"BuyTrend":          NewBuyTrend,
		"MomentumEntry":     NewMomentumEntry,
		"MidpointMomentum":  NewMidpointMomentum,
		"FadeTheMarket":     NewFadeTheMarket,
		"CheapShot":         NewCheapShot,
		"BrendaFlipper":     NewBrendaFlipper,
		"VolatilityBargain": NewVolatilityBargain,
		"BargainHunter":     NewBargainHunter,
		"GodStrategy":       NewGodStrategy,
		"DipSniper":         NewDipSniper,
	}

	marketStrategies = map[string]map[string]StrategyFactory{
		"updown": UpdownStrategies,
	}

	AllStrategies = make(map[string]StrategyFactory)
	for _, reg := range marketStrategies {
		for name, factory := range reg {
			AllStrategies[name] = factory
		}
	}
}

// ---------------------------------------------------------------------------
// BaseStrategy -- embed for tick log support
// ---------------------------------------------------------------------------

// BaseStrategy provides tick log support. Embed it in concrete strategies.
type BaseStrategy struct {
	tickLogs []string
}

// Log appends a decision log message for the current tick.
func (b *BaseStrategy) Log(msg string) {
	b.tickLogs = append(b.tickLogs, msg)
}

// DrainTickLogs returns and clears the tick log buffer.
func (b *BaseStrategy) DrainTickLogs() []string {
	out := b.tickLogs
	b.tickLogs = nil
	return out
}

// ---------------------------------------------------------------------------
// Param extraction helpers
// ---------------------------------------------------------------------------

func getFloat(params map[string]any, key string, def float64) float64 {
	if params == nil {
		return def
	}
	v, ok := params[key]
	if !ok {
		return def
	}
	switch val := v.(type) {
	case float64:
		return val
	case float32:
		return float64(val)
	case int:
		return float64(val)
	case int64:
		return float64(val)
	default:
		return def
	}
}

func getInt(params map[string]any, key string, def int) int {
	if params == nil {
		return def
	}
	v, ok := params[key]
	if !ok {
		return def
	}
	switch val := v.(type) {
	case int:
		return val
	case int64:
		return int(val)
	case float64:
		return int(val)
	case float32:
		return int(val)
	default:
		return def
	}
}

func getString(params map[string]any, key string, def string) string {
	if params == nil {
		return def
	}
	v, ok := params[key]
	if !ok {
		return def
	}
	if s, ok := v.(string); ok {
		return s
	}
	return def
}

func getFloatPtr(params map[string]any, key string) *float64 {
	if params == nil {
		return nil
	}
	v, ok := params[key]
	if !ok || v == nil {
		return nil
	}
	switch val := v.(type) {
	case float64:
		return &val
	case float32:
		f := float64(val)
		return &f
	case int:
		f := float64(val)
		return &f
	case int64:
		f := float64(val)
		return &f
	default:
		return nil
	}
}

// sampleFloat extracts a float64 from a sample map.
func sampleFloat(sample map[string]any, key string) (float64, bool) {
	v, ok := sample[key]
	if !ok || v == nil {
		return 0, false
	}
	switch val := v.(type) {
	case float64:
		return val, true
	case float32:
		return float64(val), true
	case int:
		return float64(val), true
	case int64:
		return float64(val), true
	default:
		return 0, false
	}
}
