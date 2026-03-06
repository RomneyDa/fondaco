// Package core - window_ops.go
// Shared window lifecycle operations.
// Ported from Python core/window_ops.py.

package core

import (
	"log"
	"time"
)

// WaitForNextWindow waits until the start of the next window. Returns the window timestamp.
// If the current time is within the first 3 seconds of a window, uses that window.
func WaitForNextWindow(windowDuration int, isRunning func() bool, onWaiting func(time.Time)) int {
	now := time.Now().Unix()
	currentWindowStart := (int(now) / windowDuration) * windowDuration
	nextWindowStart := currentWindowStart + windowDuration

	// If we're in the first 3 seconds of the current window, use it.
	if int(now)-currentWindowStart < 3 {
		return currentWindowStart
	}

	// Otherwise wait for the next window.
	waitSeconds := float64(nextWindowStart) - float64(now)
	nextDT := time.Unix(int64(nextWindowStart), 0).UTC()
	log.Printf("Waiting %.0fs for next window at %s", waitSeconds, nextDT.Format(time.RFC3339))

	deadline := time.Now().Add(time.Duration(waitSeconds) * time.Second)
	for time.Now().Before(deadline) && isRunning() {
		if onWaiting != nil {
			onWaiting(nextDT)
		}
		remaining := time.Until(deadline)
		sleepDur := time.Second
		if remaining < sleepDur {
			sleepDur = remaining
		}
		if sleepDur > 0 {
			time.Sleep(sleepDur)
		}
	}

	return nextWindowStart
}

// FetchMetadataWithRetry fetches market metadata with retries (market may not be created yet).
func FetchMetadataWithRetry(slugPrefix string, windowTS int, maxRetries int) *MarketMetadata {
	if maxRetries <= 0 {
		maxRetries = 5
	}
	for attempt := 0; attempt < maxRetries; attempt++ {
		metadata, err := FetchMarketMetadata(slugPrefix, windowTS)
		if err != nil {
			log.Printf("Metadata fetch attempt %d failed: %v", attempt+1, err)
		} else if metadata != nil {
			return metadata
		}
		time.Sleep(2 * time.Second)
	}
	return nil
}

// WaitForResolution polls for market resolution after the window ends.
// Returns "Up", "Down", or "" (empty string on timeout).
func WaitForResolution(slugPrefix string, windowTS int, timeout int, pollInterval float64, isRunning func() bool) string {
	deadline := time.Now().Add(time.Duration(timeout) * time.Second)

	for time.Now().Before(deadline) && isRunning() {
		outcome, err := FetchResolution(slugPrefix, windowTS)
		if err != nil {
			log.Printf("Resolution check error: %v", err)
		} else if outcome != "" {
			return outcome
		}
		time.Sleep(time.Duration(pollInterval * float64(time.Second)))
	}

	log.Printf("Resolution timeout for window %d", windowTS)
	return ""
}
