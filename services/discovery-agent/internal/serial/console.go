//go:build noxdp

// Package serial provides a fallback JSON-event logger to /dev/ttyS0 for
// environments where the HTTPS callback to api-manager is unavailable for
// more than 5 minutes.
package serial

import (
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"time"
)

const (
	serialDev     = "/dev/ttyS0"
	baudRate      = 115200
	fallbackDelay = 5 * time.Minute
)

// Event is a structured log record written to the serial console.
type Event struct {
	Time    time.Time `json:"time"`
	Level   string    `json:"level"`
	Message string    `json:"msg"`
	Fields  any       `json:"fields,omitempty"`
}

// Console writes JSON events to /dev/ttyS0 at 115200 8N1.
// If the device is unavailable, writes are silently dropped.
type Console struct {
	mu   sync.Mutex
	file *os.File
	open bool
}

var defaultConsole = &Console{}

// WriteEvent writes a structured event to the default serial console.
func WriteEvent(level, msg string, fields any) {
	defaultConsole.write(level, msg, fields)
}

func (c *Console) write(level, msg string, fields any) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if !c.open {
		if err := c.init(); err != nil {
			return
		}
	}

	ev := Event{
		Time:    time.Now().UTC(),
		Level:   level,
		Message: msg,
		Fields:  fields,
	}
	data, err := json.Marshal(ev)
	if err != nil {
		return
	}
	data = append(data, '\n')
	_, _ = c.file.Write(data)
}

// init opens /dev/ttyS0 and configures baud rate.
// Must be called with c.mu held.
func (c *Console) init() error {
	f, err := os.OpenFile(serialDev, os.O_WRONLY|syscallNoctty, 0600)
	if err != nil {
		return fmt.Errorf("open %s: %w", serialDev, err)
	}
	if err := setSerial(f, baudRate); err != nil {
		f.Close()
		return fmt.Errorf("set baud %s: %w", serialDev, err)
	}
	c.file = f
	c.open = true
	return nil
}

// Close releases the serial device handle.
func (c *Console) Close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.open && c.file != nil {
		c.file.Close()
		c.open = false
	}
}

// ShouldUseFallback returns true when the HTTPS grace period has elapsed.
// httpsFailedAt is the time at which HTTPS first failed (zero = never).
func ShouldUseFallback(httpsFailedAt time.Time) bool {
	if httpsFailedAt.IsZero() {
		return false
	}
	return time.Since(httpsFailedAt) >= fallbackDelay
}
