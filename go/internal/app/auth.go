// Package app - auth.go
// Auth handles CLOB authentication via Polygon RPC.
// TODO: Implement with go-ethereum for EIP-712 signing.

package app

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"

	"github.com/polymarket/bitcoin-5-minute/internal/core"
)

const (
	// PolygonChainID is the Polygon mainnet chain ID.
	PolygonChainID = 137

	// PolyGnosisSafe is the signature type for Polymarket Gnosis Safe proxy.
	PolyGnosisSafe = 2

	// CTFExchange is the CTF Exchange contract on Polygon.
	CTFExchange = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
)

// AuthData holds CLOB authentication credentials.
type AuthData struct {
	EOAAddress    string `json:"eoa_address"`
	SafeAddress   string `json:"safe_address"`
	APIKey        string `json:"api_key"`
	APISecret     string `json:"api_secret"`
	APIPassphrase string `json:"api_passphrase"`
}

func authCacheFile() string {
	return filepath.Join(core.BaseDir, ".cache", "auth.json")
}

// GetAuth returns Safe address and API credentials, using cache when possible.
// TODO: Implement Safe derivation and API credential creation with go-ethereum.
func GetAuth(privateKey string, forceRefresh bool) (*AuthData, error) {
	if !forceRefresh {
		cached, err := LoadAuthCache()
		if err == nil && cached != nil &&
			cached.SafeAddress != "" && cached.APIKey != "" &&
			cached.APISecret != "" && cached.APIPassphrase != "" {
			log.Printf("Auth loaded from cache (.cache/auth.json)")
			return cached, nil
		}
	}

	return nil, fmt.Errorf("auth not yet implemented - requires go-ethereum for Safe derivation and API credential creation")
}

// LoadAuthCache loads cached auth data from disk.
func LoadAuthCache() (*AuthData, error) {
	fp := authCacheFile()
	data, err := os.ReadFile(fp)
	if err != nil {
		return nil, err
	}
	var auth AuthData
	if err := json.Unmarshal(data, &auth); err != nil {
		return nil, err
	}
	return &auth, nil
}

// SaveAuthCache writes auth data to disk.
func SaveAuthCache(data *AuthData) error {
	fp := authCacheFile()
	os.MkdirAll(filepath.Dir(fp), 0o755)

	jsonData, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(fp, jsonData, 0o644)
}

// ClearAuthCache deletes the auth cache file.
func ClearAuthCache() {
	fp := authCacheFile()
	if err := os.Remove(fp); err == nil {
		log.Printf("Auth cache cleared.")
	}
}
