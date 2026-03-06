package core

// PriceSample is a single polling sample taken during a session.
// Optional float fields use *float64 to distinguish missing values from zero.
type PriceSample struct {
	TimestampUTC      string   `json:"timestamp_utc"`
	ElapsedSeconds    float64  `json:"elapsed_seconds"`
	BtcPriceUSD       *float64 `json:"btc_price_usd"`
	UpTokenMidpoint   *float64 `json:"up_token_midpoint"`
	DownTokenMidpoint *float64 `json:"down_token_midpoint"`
	UpTokenBestBid    *float64 `json:"up_token_best_bid"`
	UpTokenBestAsk    *float64 `json:"up_token_best_ask"`
	DownTokenBestBid  *float64 `json:"down_token_best_bid"`
	DownTokenBestAsk  *float64 `json:"down_token_best_ask"`
}

// MarketMetadata holds static info about a single prediction market window.
type MarketMetadata struct {
	EventID          string `json:"event_id"`
	EventSlug        string `json:"event_slug"`
	EventTitle       string `json:"event_title"`
	ConditionID      string `json:"condition_id"`
	UpTokenID        string `json:"up_token_id"`
	DownTokenID      string `json:"down_token_id"`
	WindowStartUTC   string `json:"window_start_utc"`
	WindowEndUTC     string `json:"window_end_utc"`
	ResolutionSource string `json:"resolution_source"`
}

// SessionData is the complete data for one market session.
type SessionData struct {
	Metadata             MarketMetadata `json:"metadata"`
	InitialBtcPriceUSD   *float64       `json:"initial_btc_price_usd"`
	FinalBtcPriceUSD     *float64       `json:"final_btc_price_usd"`
	Outcome              *string        `json:"outcome"`
	OutcomePrices        []string       `json:"outcome_prices"`
	Samples              []PriceSample  `json:"samples"`
	CollectionStartedUTC string         `json:"collection_started_utc"`
	CollectionEndedUTC   string         `json:"collection_ended_utc"`
	ErrorCount           int            `json:"error_count"`
	Errors               []string       `json:"errors"`
}
