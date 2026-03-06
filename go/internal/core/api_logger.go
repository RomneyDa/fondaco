// Package core - api_logger.go
// HTTP client wrapper with logging and 429 retry.
// Ported from Python core/api_logger.py.

package core

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	apiLogMaxRetries    = 4
	apiLogInitialBackoff = 1.0
	apiLogMaxBackoff     = 30.0
)

var apiLogWriteLock sync.Mutex

// LoggingClient wraps an *http.Client to log every request/response
// and retry on HTTP 429.
type LoggingClient struct {
	Client         *http.Client
	DefaultTimeout time.Duration
}

// NewLoggingClient creates a new LoggingClient with the given default timeout.
func NewLoggingClient(defaultTimeout time.Duration) *LoggingClient {
	if defaultTimeout <= 0 {
		defaultTimeout = 15 * time.Second
	}
	transport := &http.Transport{
		MaxIdleConns:        20,
		MaxIdleConnsPerHost: 20,
		IdleConnTimeout:     90 * time.Second,
	}
	return &LoggingClient{
		Client: &http.Client{
			Transport: transport,
			Timeout:   defaultTimeout,
		},
		DefaultTimeout: defaultTimeout,
	}
}

// Do executes an HTTP request with retry on 429 and logging.
func (c *LoggingClient) Do(req *http.Request) (*http.Response, error) {
	parsed, _ := url.Parse(req.URL.String())
	host := ""
	if parsed != nil {
		host = parsed.Hostname()
	}
	path := req.URL.Path

	backoff := apiLogInitialBackoff
	var lastResp *http.Response

	for attempt := 1; attempt <= apiLogMaxRetries+1; attempt++ {
		ts := time.Now().UTC()
		start := time.Now()

		entry := map[string]interface{}{
			"timestamp_utc": ts.Format(time.RFC3339Nano),
			"method":        req.Method,
			"url":           req.URL.String(),
			"host":          host,
			"path":          path,
			"attempt":       attempt,
		}

		resp, err := c.Client.Do(req)
		elapsedMs := float64(time.Since(start).Milliseconds())
		entry["elapsed_ms"] = elapsedMs

		if err != nil {
			entry["status_code"] = nil
			entry["response_body"] = nil
			entry["error"] = fmt.Sprintf("%T: %v", err, err)
			if host != "clob.polymarket.com" {
				writeLogEntry(host, ts, entry)
			}
			return nil, err
		}

		entry["status_code"] = resp.StatusCode
		entry["error"] = nil

		// Read body for logging (skip for clob.polymarket.com).
		if host != "clob.polymarket.com" {
			bodyBytes, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			resp.Body = io.NopCloser(strings.NewReader(string(bodyBytes)))

			var bodyJSON interface{}
			if err := json.Unmarshal(bodyBytes, &bodyJSON); err == nil {
				entry["response_body"] = bodyJSON
			} else {
				bodyStr := string(bodyBytes)
				if len(bodyStr) > 10000 {
					bodyStr = bodyStr[:10000] + "...[truncated]"
				}
				entry["response_body"] = bodyStr
			}
			writeLogEntry(host, ts, entry)
		}

		lastResp = resp

		// Handle 429 rate limit.
		if resp.StatusCode == 429 && attempt <= apiLogMaxRetries {
			retryAfter := parseRetryAfter(resp)
			wait := backoff
			if retryAfter > 0 {
				wait = retryAfter
			}
			jitter := 0.5 + rand.Float64() // 0.5 to 1.5
			wait *= jitter
			log.Printf("Rate limited (429) on %s %s (attempt %d), retrying in %.1fs",
				req.Method, path, attempt, wait)
			time.Sleep(time.Duration(wait * float64(time.Second)))
			backoff *= 2
			if backoff > apiLogMaxBackoff {
				backoff = apiLogMaxBackoff
			}
			continue
		}

		return resp, nil
	}

	return lastResp, nil
}

// Get performs an HTTP GET request.
func (c *LoggingClient) Get(urlStr string) (*http.Response, error) {
	req, err := http.NewRequest("GET", urlStr, nil)
	if err != nil {
		return nil, err
	}
	return c.Do(req)
}

// Post performs an HTTP POST request.
func (c *LoggingClient) Post(urlStr string, contentType string, body io.Reader) (*http.Response, error) {
	req, err := http.NewRequest("POST", urlStr, body)
	if err != nil {
		return nil, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	return c.Do(req)
}

// Close closes the underlying HTTP client's idle connections.
func (c *LoggingClient) Close() {
	c.Client.CloseIdleConnections()
}

func parseRetryAfter(resp *http.Response) float64 {
	value := resp.Header.Get("Retry-After")
	if value == "" {
		return 0
	}
	f, err := strconv.ParseFloat(value, 64)
	if err != nil {
		return 0
	}
	return f
}

func writeLogEntry(host string, ts time.Time, entry map[string]interface{}) {
	if host == "" {
		host = "unknown"
	}
	dateStr := ts.Format("2006-01-02")
	hostDir := filepath.Join(APIRawDir(), host)
	if err := os.MkdirAll(hostDir, 0o755); err != nil {
		return
	}
	fp := filepath.Join(hostDir, dateStr+".jsonl")

	line, err := json.Marshal(entry)
	if err != nil {
		return
	}
	line = append(line, '\n')

	apiLogWriteLock.Lock()
	defer apiLogWriteLock.Unlock()

	f, err := os.OpenFile(fp, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		log.Printf("Failed to write API log entry: %v", err)
		return
	}
	defer f.Close()
	f.Write(line)
}
