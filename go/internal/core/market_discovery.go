// Package core - market_discovery.go
// Market discovery and resolution checking via the Gamma Markets API.
// Ported from Python core/market_discovery.py.

package core

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"
)

var (
	mdClient   *LoggingClient
	mdClientMu sync.Mutex
)

// SetClient sets the shared LoggingClient used for all market discovery requests.
func SetClient(client *LoggingClient) {
	mdClientMu.Lock()
	defer mdClientMu.Unlock()
	mdClient = client
}

func getClient() *LoggingClient {
	mdClientMu.Lock()
	defer mdClientMu.Unlock()
	if mdClient != nil {
		return mdClient
	}
	return NewLoggingClient(15 * time.Second)
}

// ComputeWindowTimestamp computes the window start unix timestamp for a given UTC time.
// If utcTime is nil, uses the current time.
func ComputeWindowTimestamp(windowDuration int, utcTime *float64) int {
	var t float64
	if utcTime != nil {
		t = *utcTime
	} else {
		t = float64(time.Now().Unix())
	}
	return (int(t) / windowDuration) * windowDuration
}

// GetEventSlug builds the event slug from a prefix and window start timestamp.
func GetEventSlug(slugPrefix string, windowStartTS int) string {
	return fmt.Sprintf("%s%d", slugPrefix, windowStartTS)
}

// FetchMarketMetadata fetches the market metadata for a specific window.
// Returns nil if the event does not exist yet or is closed.
func FetchMarketMetadata(slugPrefix string, windowStartTS int) (*MarketMetadata, error) {
	slug := GetEventSlug(slugPrefix, windowStartTS)
	client := getClient()

	reqURL := fmt.Sprintf("%s/events?slug=%s", GammaAPIBase, slug)
	req, err := http.NewRequest("GET", reqURL, nil)
	if err != nil {
		return nil, err
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("gamma API returned status %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var events []map[string]interface{}
	if err := json.Unmarshal(body, &events); err != nil {
		return nil, err
	}

	if len(events) == 0 {
		return nil, nil
	}

	event := events[0]
	marketsRaw, ok := event["markets"]
	if !ok {
		return nil, nil
	}
	markets, ok := marketsRaw.([]interface{})
	if !ok || len(markets) == 0 {
		return nil, nil
	}

	market, ok := markets[0].(map[string]interface{})
	if !ok {
		return nil, nil
	}

	if closed, _ := market["closed"].(bool); closed {
		return nil, nil
	}

	// Parse clobTokenIds JSON string.
	clobTokenIDsStr, _ := market["clobTokenIds"].(string)
	var clobTokenIDs []string
	if err := json.Unmarshal([]byte(clobTokenIDsStr), &clobTokenIDs); err != nil {
		return nil, fmt.Errorf("failed to parse clobTokenIds: %v", err)
	}
	if len(clobTokenIDs) < 2 {
		return nil, fmt.Errorf("expected at least 2 clobTokenIds, got %d", len(clobTokenIDs))
	}

	eventID, _ := event["id"].(string)
	eventSlug, _ := event["slug"].(string)
	eventTitle, _ := event["title"].(string)
	conditionID, _ := market["conditionId"].(string)

	windowStartUTC, _ := market["eventStartTime"].(string)
	if windowStartUTC == "" {
		windowStartUTC, _ = market["startDate"].(string)
	}
	windowEndUTC, _ := market["endDate"].(string)
	resolutionSource, _ := market["resolutionSource"].(string)

	return &MarketMetadata{
		EventID:          eventID,
		EventSlug:        eventSlug,
		EventTitle:       eventTitle,
		ConditionID:      conditionID,
		UpTokenID:        clobTokenIDs[0],
		DownTokenID:      clobTokenIDs[1],
		WindowStartUTC:   windowStartUTC,
		WindowEndUTC:     windowEndUTC,
		ResolutionSource: resolutionSource,
	}, nil
}

// FetchResolution checks if a market has resolved.
// Returns "Up", "Down", or "" (empty string if not resolved).
func FetchResolution(slugPrefix string, windowStartTS int) (string, error) {
	slug := GetEventSlug(slugPrefix, windowStartTS)
	client := getClient()

	reqURL := fmt.Sprintf("%s/events?slug=%s", GammaAPIBase, slug)
	req, err := http.NewRequest("GET", reqURL, nil)
	if err != nil {
		return "", err
	}

	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return "", fmt.Errorf("gamma API returned status %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}

	var events []map[string]interface{}
	if err := json.Unmarshal(body, &events); err != nil {
		return "", err
	}

	if len(events) == 0 {
		return "", nil
	}

	marketsRaw, ok := events[0]["markets"]
	if !ok {
		return "", nil
	}
	markets, ok := marketsRaw.([]interface{})
	if !ok || len(markets) == 0 {
		return "", nil
	}

	market, ok := markets[0].(map[string]interface{})
	if !ok {
		return "", nil
	}

	if closed, _ := market["closed"].(bool); !closed {
		return "", nil
	}

	outcomePricesRaw, ok := market["outcomePrices"]
	if !ok || outcomePricesRaw == nil {
		return "", nil
	}

	var prices []string
	switch v := outcomePricesRaw.(type) {
	case string:
		if err := json.Unmarshal([]byte(v), &prices); err != nil {
			return "", nil
		}
	case []interface{}:
		for _, p := range v {
			if s, ok := p.(string); ok {
				prices = append(prices, s)
			}
		}
	default:
		return "", nil
	}

	if len(prices) < 2 {
		return "", nil
	}

	if prices[0] == "1" {
		return "Up", nil
	}
	if prices[1] == "1" {
		return "Down", nil
	}

	return "", nil
}
