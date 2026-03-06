package strategy

import (
	"fmt"
	"sort"
	"time"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

// BuyTrend checks recent session history for a BTC price trend and buys
// in that direction at the start of the session.
type BuyTrend struct {
	BaseStrategy
	MinSessions     int
	LookbackMinutes int
	MinChangeBps    float64
	MinDirectionPct float64
	BetFraction     float64

	shouldBuy string
	bought    bool
}

// NewBuyTrend creates a BuyTrend from params.
func NewBuyTrend(params map[string]any) Strategy {
	return &BuyTrend{
		MinSessions:     getInt(params, "min_sessions", 5),
		LookbackMinutes: getInt(params, "lookback_minutes", 60),
		MinChangeBps:    getFloat(params, "min_change_bps", 5.0),
		MinDirectionPct: getFloat(params, "min_direction_pct", 0.75),
		BetFraction:     getFloat(params, "bet_fraction", 1.0),
	}
}

func (s *BuyTrend) Name() string { return "BuyTrend" }

// parseUTC parses an ISO timestamp, handling the 'Z' suffix.
func parseUTC(str string) (time.Time, error) {
	formats := []string{
		time.RFC3339,
		"2006-01-02T15:04:05",
		time.RFC3339Nano,
	}
	for _, f := range formats {
		if t, err := time.Parse(f, str); err == nil {
			if t.Location() == time.UTC || t.Location() == nil {
				return t.UTC(), nil
			}
			return t.UTC(), nil
		}
	}
	return time.Time{}, fmt.Errorf("cannot parse timestamp: %s", str)
}

type sessionSummary struct {
	WindowStart time.Time
	InitialBtc  float64
	FinalBtc    float64
	Outcome     string
}

func (s *BuyTrend) OnSessionStart(metadata map[string]any) {
	s.shouldBuy = ""
	s.bought = false

	// Parse current session's window start
	windowStartStr, _ := metadata["window_start_utc"].(string)
	if windowStartStr == "" {
		return
	}
	currentStart, err := parseUTC(windowStartStr)
	if err != nil {
		s.Log(fmt.Sprintf("Cannot parse window_start_utc: %s", windowStartStr))
		return
	}

	cutoff := currentStart.Add(-time.Duration(s.LookbackMinutes) * time.Minute)

	// Use injected summaries from backtest engine if available
	var summaries []sessionSummary
	if prior, ok := metadata["_prior_session_summaries"]; ok {
		if priorList, ok := prior.([]map[string]any); ok {
			for _, m := range priorList {
				ws, _ := m["window_start"].(time.Time)
				ib, _ := m["initial_btc"].(float64)
				fb, _ := m["final_btc"].(float64)
				outcome, _ := m["outcome"].(string)
				if ib > 0 && fb > 0 {
					summaries = append(summaries, sessionSummary{
						WindowStart: ws,
						InitialBtc:  ib,
						FinalBtc:    fb,
						Outcome:     outcome,
					})
				}
			}
		}
	}

	// Filter to sessions in the lookback window, strictly before current
	var recent []sessionSummary
	for _, ss := range summaries {
		if !ss.WindowStart.Before(cutoff) && ss.WindowStart.Before(currentStart) {
			recent = append(recent, ss)
		}
	}

	sort.Slice(recent, func(i, j int) bool {
		return recent[i].WindowStart.Before(recent[j].WindowStart)
	})

	if len(recent) < s.MinSessions {
		s.Log(fmt.Sprintf("Only %d sessions in lookback (need %d)", len(recent), s.MinSessions))
		return
	}

	// Require contiguous sessions (max 7min gap)
	maxGap := 7 * time.Minute
	for i := 1; i < len(recent); i++ {
		gap := recent[i].WindowStart.Sub(recent[i-1].WindowStart)
		if gap > maxGap {
			s.Log(fmt.Sprintf("Gap in session data: %s -> %s (%.0fmin gap), holding off",
				recent[i-1].WindowStart.Format("15:04"),
				recent[i].WindowStart.Format("15:04"),
				gap.Minutes()))
			return
		}
	}

	// Require most recent session is close to current
	lastToCurrent := currentStart.Sub(recent[len(recent)-1].WindowStart)
	if lastToCurrent > maxGap {
		s.Log(fmt.Sprintf("Most recent session %dmin before current, holding off",
			int(lastToCurrent.Minutes())))
		return
	}

	// Overall BTC price change
	firstPrice := recent[0].InitialBtc
	lastPrice := recent[len(recent)-1].FinalBtc
	if firstPrice <= 0 {
		return
	}

	changeBps := ((lastPrice - firstPrice) / firstPrice) * 10000.0

	if abs(changeBps) < s.MinChangeBps {
		s.Log(fmt.Sprintf("BTC change %.1fbps < threshold %.1fbps", changeBps, s.MinChangeBps))
		return
	}

	// Check direction consistency
	trendUp := changeBps > 0
	matching := 0
	for _, ss := range recent {
		if (ss.Outcome == "Up") == trendUp {
			matching++
		}
	}
	directionPct := float64(matching) / float64(len(recent))

	if directionPct < s.MinDirectionPct {
		s.Log(fmt.Sprintf("Direction consistency %.0f%% < threshold %.0f%%",
			directionPct*100, s.MinDirectionPct*100))
		return
	}

	if trendUp {
		s.shouldBuy = "buy_up"
	} else {
		s.shouldBuy = "buy_down"
	}
	sign := "+"
	if changeBps < 0 {
		sign = ""
	}
	s.Log(fmt.Sprintf("Trend: BTC %s%.1fbps over %d sessions, %.0f%% confirm -> %s",
		sign, changeBps, len(recent), directionPct*100, s.shouldBuy))
}

func abs(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}

func (s *BuyTrend) OnTick(ctx core.TickContext) core.TradeAction {
	if s.shouldBuy != "" && !s.bought {
		s.bought = true
		var price float64
		if s.shouldBuy == "buy_up" {
			price, _ = sampleFloat(ctx.Sample, "up_token_midpoint")
		} else {
			price, _ = sampleFloat(ctx.Sample, "down_token_midpoint")
		}
		return core.TradeAction{
			Side:   s.shouldBuy,
			Amount: ctx.Position.Cash * s.BetFraction,
			Price:  &price,
		}
	}
	return core.Hold
}

func (s *BuyTrend) ParamGrid() map[string][]any {
	return map[string][]any{
		"min_sessions":     {3, 5, 7, 10},
		"lookback_minutes": {30, 60, 120},
		"min_change_bps":   {3.0, 5.0, 10.0, 20.0, 50.0},
		"min_direction_pct": {0.5, 0.6, 0.75, 0.9},
		"bet_fraction":     {0.5, 0.75, 1.0},
	}
}
