// Package core - price_fetcher.go
// BTC spot price and Polymarket token price fetching.
// Ported from Python core/price_fetcher.py.

package core

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"sync"
	"time"
)

const futureTimeout = 10 * time.Second

// PriceFetcher fetches BTC spot price and Polymarket token prices.
type PriceFetcher struct {
	client       *LoggingClient
	rtds         *RTDSClient
	lastBtcPrice *float64
	mu           sync.Mutex
}

// NewPriceFetcher creates a new PriceFetcher.
func NewPriceFetcher(client *LoggingClient, rtds *RTDSClient) *PriceFetcher {
	if client == nil {
		client = NewLoggingClient(15 * time.Second)
	}
	return &PriceFetcher{
		client: client,
		rtds:   rtds,
	}
}

// GetBtcPrice gets the current BTC/USD price.
// Priority: RTDS Chainlink -> Coinbase -> CoinGecko.
func (f *PriceFetcher) GetBtcPrice() *float64 {
	// Try RTDS first.
	if f.rtds != nil {
		price := f.rtds.GetPrice(0)
		if price != nil {
			return price
		}
	}

	// Coinbase fallback.
	req, err := http.NewRequest("GET", CoinbaseBtcURL, nil)
	if err == nil {
		req.Header.Set("Accept", "application/json")
		resp, err := f.client.Do(req)
		if err == nil {
			defer resp.Body.Close()
			if resp.StatusCode == 200 {
				body, _ := io.ReadAll(resp.Body)
				var result struct {
					Data struct {
						Amount string `json:"amount"`
					} `json:"data"`
				}
				if json.Unmarshal(body, &result) == nil && result.Data.Amount != "" {
					var price float64
					if _, err := fmt.Sscanf(result.Data.Amount, "%f", &price); err == nil {
						return &price
					}
				}
			}
		}
	}

	// CoinGecko fallback.
	req, err = http.NewRequest("GET", CoingeckoBtcURL, nil)
	if err == nil {
		req.Header.Set("Accept", "application/json")
		resp, err := f.client.Do(req)
		if err == nil {
			defer resp.Body.Close()
			if resp.StatusCode == 200 {
				body, _ := io.ReadAll(resp.Body)
				var result map[string]map[string]float64
				if json.Unmarshal(body, &result) == nil {
					if btc, ok := result["bitcoin"]; ok {
						if usd, ok := btc["usd"]; ok {
							return &usd
						}
					}
				}
			}
		}
	}

	log.Printf("Failed to fetch BTC price from all sources")
	return nil
}

// GetTokenMidpoint gets the midpoint price for a token from the CLOB API.
func (f *PriceFetcher) GetTokenMidpoint(tokenID string) *float64 {
	reqURL := fmt.Sprintf("%s/midpoint?token_id=%s", ClobAPIBase, tokenID)
	req, err := http.NewRequest("GET", reqURL, nil)
	if err != nil {
		return nil
	}
	req.Header.Set("Accept", "application/json")

	resp, err := f.client.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return nil
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil
	}

	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil
	}

	midRaw, ok := result["mid"]
	if !ok || midRaw == nil {
		return nil
	}

	switch v := midRaw.(type) {
	case float64:
		return &v
	case string:
		var mid float64
		if _, err := fmt.Sscanf(v, "%f", &mid); err == nil {
			return &mid
		}
	}
	return nil
}

// GetTokenBookTop gets the best bid and ask for a token.
// Returns (bestBid, bestAsk) where either may be nil.
func (f *PriceFetcher) GetTokenBookTop(tokenID string) (*float64, *float64) {
	reqURL := fmt.Sprintf("%s/book?token_id=%s", ClobAPIBase, tokenID)
	req, err := http.NewRequest("GET", reqURL, nil)
	if err != nil {
		return nil, nil
	}
	req.Header.Set("Accept", "application/json")

	resp, err := f.client.Do(req)
	if err != nil {
		return nil, nil
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return nil, nil
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, nil
	}

	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, nil
	}

	var bestBid, bestAsk *float64

	if bids, ok := result["bids"].([]interface{}); ok && len(bids) > 0 {
		if bid, ok := bids[0].(map[string]interface{}); ok {
			if priceStr, ok := bid["price"].(string); ok {
				var p float64
				if _, err := fmt.Sscanf(priceStr, "%f", &p); err == nil {
					bestBid = &p
				}
			} else if priceF, ok := bid["price"].(float64); ok {
				bestBid = &priceF
			}
		}
	}

	if asks, ok := result["asks"].([]interface{}); ok && len(asks) > 0 {
		if ask, ok := asks[0].(map[string]interface{}); ok {
			if priceStr, ok := ask["price"].(string); ok {
				var p float64
				if _, err := fmt.Sscanf(priceStr, "%f", &p); err == nil {
					bestAsk = &p
				}
			} else if priceF, ok := ask["price"].(float64); ok {
				bestAsk = &priceF
			}
		}
	}

	return bestBid, bestAsk
}

// TakeSample takes a complete price sample, firing 4 CLOB requests in parallel.
func (f *PriceFetcher) TakeSample(upTokenID, downTokenID, windowStartUTC string) PriceSample {
	now := time.Now().UTC()

	// Parse window start to compute elapsed.
	windowStart, err := time.Parse(time.RFC3339, windowStartUTC)
	if err != nil {
		// Try alternative format.
		windowStart, err = time.Parse("2006-01-02T15:04:05Z", windowStartUTC)
		if err != nil {
			windowStart, _ = time.Parse("2006-01-02T15:04:05+00:00", windowStartUTC)
		}
	}
	elapsed := now.Sub(windowStart).Seconds()

	// Get BTC price (from RTDS, zero-cost).
	var btcPrice *float64
	if f.rtds != nil {
		btcPrice = f.rtds.GetPrice(0)
	}
	if btcPrice != nil {
		f.mu.Lock()
		f.lastBtcPrice = btcPrice
		f.mu.Unlock()
	} else {
		f.mu.Lock()
		btcPrice = f.lastBtcPrice
		f.mu.Unlock()
		if btcPrice == nil {
			btcPrice = f.GetBtcPrice()
			if btcPrice != nil {
				f.mu.Lock()
				f.lastBtcPrice = btcPrice
				f.mu.Unlock()
			}
		}
	}

	// Fire 4 CLOB requests in parallel.
	type midResult struct {
		val *float64
	}
	type bookResult struct {
		bid *float64
		ask *float64
	}

	var wg sync.WaitGroup
	var upMid, downMid midResult
	var upBook, downBook bookResult

	wg.Add(4)
	go func() {
		defer wg.Done()
		upMid.val = f.GetTokenMidpoint(upTokenID)
	}()
	go func() {
		defer wg.Done()
		downMid.val = f.GetTokenMidpoint(downTokenID)
	}()
	go func() {
		defer wg.Done()
		upBook.bid, upBook.ask = f.GetTokenBookTop(upTokenID)
	}()
	go func() {
		defer wg.Done()
		downBook.bid, downBook.ask = f.GetTokenBookTop(downTokenID)
	}()

	wg.Wait()

	return PriceSample{
		TimestampUTC:      now.Format(time.RFC3339Nano),
		ElapsedSeconds:    math.Round(elapsed*100) / 100,
		BtcPriceUSD:       btcPrice,
		UpTokenMidpoint:   upMid.val,
		DownTokenMidpoint: downMid.val,
		UpTokenBestBid:    upBook.bid,
		UpTokenBestAsk:    upBook.ask,
		DownTokenBestBid:  downBook.bid,
		DownTokenBestAsk:  downBook.ask,
	}
}

// Close closes the underlying HTTP client.
func (f *PriceFetcher) Close() {
	f.client.Close()
}
