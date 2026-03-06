// Package core - tick_broadcast.go
// Unix domain socket broadcaster and subscriber for tick data.
// Ported from Python core/tick_broadcast.py.

package core

import (
	"bufio"
	"encoding/json"
	"log"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// TickBroadcaster broadcasts tick messages over a Unix domain socket.
// Accepts multiple client connections and sends JSON lines to all of them.
// Non-blocking sends -- slow/dead clients get dropped.
type TickBroadcaster struct {
	socketPath string
	listener   net.Listener
	clients    []net.Conn
	mu         sync.Mutex
	running    bool
}

// NewTickBroadcaster creates a new broadcaster for the given socket path.
func NewTickBroadcaster(socketPath string) *TickBroadcaster {
	return &TickBroadcaster{
		socketPath: socketPath,
	}
}

// Start removes any stale socket file and begins listening for connections.
func (b *TickBroadcaster) Start() error {
	// Remove stale socket file.
	os.Remove(b.socketPath)
	if err := os.MkdirAll(filepath.Dir(b.socketPath), 0o755); err != nil {
		return err
	}

	listener, err := net.Listen("unix", b.socketPath)
	if err != nil {
		return err
	}
	b.listener = listener
	b.running = true

	go b.acceptLoop()
	log.Printf("TickBroadcaster listening on %s", b.socketPath)
	return nil
}

// Stop closes all client connections, closes the listener, and removes the socket file.
func (b *TickBroadcaster) Stop() {
	b.running = false
	b.mu.Lock()
	for _, c := range b.clients {
		c.Close()
	}
	b.clients = nil
	b.mu.Unlock()

	if b.listener != nil {
		b.listener.Close()
	}
	os.Remove(b.socketPath)
}

// Broadcast sends a message to all connected clients as a JSON line.
// Dead clients are automatically dropped.
func (b *TickBroadcaster) Broadcast(message map[string]interface{}) {
	data, err := json.Marshal(message)
	if err != nil {
		log.Printf("TickBroadcaster: marshal error: %v", err)
		return
	}
	data = append(data, '\n')

	b.mu.Lock()
	defer b.mu.Unlock()

	var alive []net.Conn
	deadCount := 0
	for _, c := range b.clients {
		c.SetWriteDeadline(time.Now().Add(1 * time.Second))
		_, err := c.Write(data)
		if err != nil {
			c.Close()
			deadCount++
		} else {
			alive = append(alive, c)
		}
	}
	b.clients = alive
	if deadCount > 0 {
		log.Printf("TickBroadcaster: dropped %d dead client(s), %d remaining", deadCount, len(b.clients))
	}
}

// ClientCount returns the number of currently connected clients.
func (b *TickBroadcaster) ClientCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.clients)
}

func (b *TickBroadcaster) acceptLoop() {
	for b.running {
		conn, err := b.listener.Accept()
		if err != nil {
			if b.running {
				time.Sleep(100 * time.Millisecond)
			}
			continue
		}
		b.mu.Lock()
		b.clients = append(b.clients, conn)
		count := len(b.clients)
		b.mu.Unlock()
		log.Printf("TickBroadcaster: client connected (%d total)", count)
	}
}

// TickSubscriber connects to a broadcaster's Unix socket and reads JSON lines.
// Handles reconnection automatically if the broadcaster restarts.
type TickSubscriber struct {
	socketPath     string
	reconnectDelay time.Duration
	conn           net.Conn
	scanner        *bufio.Scanner
}

// NewTickSubscriber creates a subscriber for the given socket path.
func NewTickSubscriber(socketPath string, reconnectDelay time.Duration) *TickSubscriber {
	if reconnectDelay <= 0 {
		reconnectDelay = 2 * time.Second
	}
	return &TickSubscriber{
		socketPath:     socketPath,
		reconnectDelay: reconnectDelay,
	}
}

// Connect establishes a connection to the broadcaster socket.
func (s *TickSubscriber) Connect() error {
	conn, err := net.Dial("unix", s.socketPath)
	if err != nil {
		return err
	}
	s.conn = conn
	s.scanner = bufio.NewScanner(conn)
	s.scanner.Buffer(make([]byte, 256*1024), 256*1024)
	log.Printf("TickSubscriber connected to %s", s.socketPath)
	return nil
}

// Close closes the connection.
func (s *TickSubscriber) Close() {
	if s.conn != nil {
		s.conn.Close()
		s.conn = nil
	}
	s.scanner = nil
}

// Messages returns a channel that yields message maps. It reads JSON lines
// from the socket, reconnecting on disconnect. The goroutine stops when
// isRunning returns false. The channel is closed when the goroutine exits.
func (s *TickSubscriber) Messages(isRunning func() bool) <-chan map[string]interface{} {
	ch := make(chan map[string]interface{}, 64)

	go func() {
		defer close(ch)
		for isRunning() {
			if s.conn == nil {
				if err := s.Connect(); err != nil {
					log.Printf("TickSubscriber: waiting for broadcaster...")
					time.Sleep(s.reconnectDelay)
					continue
				}
			}

			if !s.scanner.Scan() {
				err := s.scanner.Err()
				if err != nil {
					log.Printf("TickSubscriber disconnected: %v. Reconnecting...", err)
				} else {
					log.Printf("TickSubscriber: server closed connection. Reconnecting...")
				}
				s.Close()
				time.Sleep(s.reconnectDelay)
				continue
			}

			line := s.scanner.Bytes()
			if len(line) == 0 {
				continue
			}

			var msg map[string]interface{}
			if err := json.Unmarshal(line, &msg); err != nil {
				log.Printf("TickSubscriber: JSON parse error: %v", err)
				continue
			}

			select {
			case ch <- msg:
			default:
				// Channel full, drop message.
			}
		}
	}()

	return ch
}
