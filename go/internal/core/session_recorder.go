// Package core - session_recorder.go
// Session data persistence and collection logging.
// Ported from Python core/session_recorder.py.

package core

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// SessionRecorder manages writing session data to disk.
type SessionRecorder struct {
	SlugKey      string
	MarketDir    string // sessions directory
	liveDir      string
	LiveFilepath string
	logFile      string
}

// NewSessionRecorder creates a SessionRecorder for the given slug prefix.
func NewSessionRecorder(slugPrefix string, sessionsDir string, liveDir string, logFile string) *SessionRecorder {
	slugKey := SlugKey(slugPrefix)

	if sessionsDir == "" {
		sessionsDir = MarketSessionsDir(slugKey)
	}
	if liveDir == "" {
		liveDir = MarketLiveDir(slugKey)
	}
	if logFile == "" {
		logFile = MarketSessionLog(slugKey)
	}

	liveFilepath := filepath.Join(liveDir, "session.json")

	os.MkdirAll(sessionsDir, 0o755)
	os.MkdirAll(liveDir, 0o755)
	os.MkdirAll(filepath.Dir(logFile), 0o755)

	return &SessionRecorder{
		SlugKey:      slugKey,
		MarketDir:    sessionsDir,
		liveDir:      liveDir,
		LiveFilepath: liveFilepath,
		logFile:      logFile,
	}
}

// SessionFilepath generates the file path for a session.
// Colons are replaced for filesystem safety.
func (r *SessionRecorder) SessionFilepath(windowStartUTC string) string {
	safeName := strings.ReplaceAll(windowStartUTC, ":", "-")
	return filepath.Join(r.MarketDir, safeName+".json")
}

// SessionExists checks if a session file already exists.
func (r *SessionRecorder) SessionExists(windowStartUTC string) bool {
	_, err := os.Stat(r.SessionFilepath(windowStartUTC))
	return err == nil
}

// SaveSession writes session data to a JSON file and returns the file path.
func (r *SessionRecorder) SaveSession(session *SessionData) string {
	fp := r.SessionFilepath(session.Metadata.WindowStartUTC)
	data, err := session.ToJSON()
	if err != nil {
		log.Printf("Failed to serialize session: %v", err)
		return fp
	}
	if err := os.WriteFile(fp, data, 0o644); err != nil {
		log.Printf("Failed to write session file: %v", err)
	}
	return fp
}

// AppendLog appends a line to the collection log.
func (r *SessionRecorder) AppendLog(message string, extra map[string]interface{}) {
	entry := map[string]interface{}{
		"timestamp": time.Now().UTC().Format(time.RFC3339),
		"message":   message,
	}
	for k, v := range extra {
		entry[k] = v
	}
	line, err := json.Marshal(entry)
	if err != nil {
		return
	}
	line = append(line, '\n')

	f, err := os.OpenFile(r.logFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		log.Printf("Failed to write log entry: %v", err)
		return
	}
	defer f.Close()
	f.Write(line)
}

// WriteLiveStatus writes a status-only live file.
func (r *SessionRecorder) WriteLiveStatus(status string, nextWindowUTC string, tradingInfo map[string]interface{}) {
	entry := map[string]interface{}{
		"status":          status,
		"slug_key":        r.SlugKey,
		"timestamp":       time.Now().UTC().Format(time.RFC3339),
		"next_window_utc": nextWindowUTC,
	}
	if tradingInfo != nil {
		entry["trading"] = tradingInfo
	}
	data, err := json.MarshalIndent(entry, "", "  ")
	if err != nil {
		return
	}
	os.WriteFile(r.LiveFilepath, data, 0o644)
}

// WriteLiveSession writes in-progress session data to the live directory.
func (r *SessionRecorder) WriteLiveSession(session *SessionData, tradingInfo map[string]interface{}) {
	sessionJSON, err := session.ToJSON()
	if err != nil {
		return
	}
	var data map[string]interface{}
	if err := json.Unmarshal(sessionJSON, &data); err != nil {
		return
	}
	if tradingInfo != nil {
		data["trading"] = tradingInfo
	}
	output, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return
	}
	os.WriteFile(r.LiveFilepath, output, 0o644)
}

// ClearLiveSession removes the live session file.
func (r *SessionRecorder) ClearLiveSession() {
	os.Remove(r.LiveFilepath)
}

// CountCompletedSessions counts how many session JSON files exist.
func (r *SessionRecorder) CountCompletedSessions() int {
	entries, err := os.ReadDir(r.MarketDir)
	if err != nil {
		return 0
	}
	count := 0
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".json") {
			count++
		}
	}
	return count
}

// StringPtr is a helper to create a string pointer.
func StringPtr(s string) *string {
	return &s
}

// FormatFloat64 formats a float64 pointer for display.
func FormatFloat64(v *float64) string {
	if v == nil {
		return "nil"
	}
	return fmt.Sprintf("%.2f", *v)
}
