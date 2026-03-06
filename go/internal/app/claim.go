// Package app - claim.go
// Claim handles redeeming resolved positions via Builder Relayer.
// TODO: Implement with go-ethereum for ABI encoding.

package app

import (
	"log"
	"os"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

const (
	// CTFContract is the Conditional Token Framework contract on Polygon.
	CTFContract = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

	// USDCE is the USDC.e token on Polygon (collateral for all Polymarket markets).
	USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

	// BuilderRelayerURL is the Builder Relayer endpoint for gasless Safe transactions.
	BuilderRelayerURL = "https://relayer-v2.polymarket.com"

	// DataAPIBase is the Polymarket data API for querying positions.
	DataAPIBase = "https://data-api.polymarket.com"
)

// HasBuilderCreds checks if Builder API credentials are configured in env.
func HasBuilderCreds() bool {
	return os.Getenv("POLYMARKET_BUILDER_API_KEY") != "" &&
		os.Getenv("POLYMARKET_BUILDER_SECRET") != "" &&
		os.Getenv("POLYMARKET_BUILDER_PASSPHRASE") != ""
}

// FetchRedeemablePositions queries the data API for redeemable positions.
// Returns nil on any error.
func FetchRedeemablePositions(safeAddress string) ([]map[string]interface{}, error) {
	client := core.NewLoggingClient(0)
	defer client.Close()

	// TODO: Implement HTTP call to DataAPIBase/positions with query params.
	// This is a stub -- full implementation requires the data API client.
	log.Printf("FetchRedeemablePositions: stub -- not yet implemented")
	return nil, nil
}

// ClaimRedeemable claims all redeemable positions.
// Returns (succeeded, failed, totalValue, error).
// TODO: Implement with go-ethereum for ABI encoding and Builder Relayer client.
func ClaimRedeemable(privateKey, safeAddress string, waitForConfirm bool) (int, int, float64, error) {
	positions, err := FetchRedeemablePositions(safeAddress)
	if err != nil {
		return 0, 0, 0, err
	}
	if len(positions) == 0 {
		return 0, 0, 0, nil
	}

	totalValue := 0.0
	for _, pos := range positions {
		if size, ok := pos["size"].(float64); ok {
			totalValue += size
		}
	}

	log.Printf("Found %d redeemable position(s) (~$%.2f)", len(positions), totalValue)
	log.Printf("ClaimRedeemable: stub -- redemption not yet implemented (requires go-ethereum)")

	return 0, 0, totalValue, nil
}

// ClaimBackground runs claim_redeemable in a goroutine (fire-and-forget).
func ClaimBackground(privateKey, safeAddress string) {
	go func() {
		s, f, val, _ := ClaimRedeemable(privateKey, safeAddress, false)
		if s > 0 || f > 0 {
			log.Printf("Background claim: %d submitted, %d failed (~$%.2f)", s, f, val)
		}
	}()
}
