//go:build noxdp

package serial_test

import (
	"testing"
	"time"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/serial"
)

func TestShouldUseFallback_ZeroTime(t *testing.T) {
	if serial.ShouldUseFallback(time.Time{}) {
		t.Error("ShouldUseFallback(zero) should return false")
	}
}

func TestShouldUseFallback_Recent(t *testing.T) {
	if serial.ShouldUseFallback(time.Now()) {
		t.Error("ShouldUseFallback(now) should return false (grace period not elapsed)")
	}
}

func TestShouldUseFallback_OldFailure(t *testing.T) {
	// Simulate a failure 10 minutes ago.
	failedAt := time.Now().Add(-10 * time.Minute)
	if !serial.ShouldUseFallback(failedAt) {
		t.Error("ShouldUseFallback(10 min ago) should return true")
	}
}

func TestWriteEvent_NoSysfs_DoesNotPanic(t *testing.T) {
	// WriteEvent on a machine without /dev/ttyS0 should drop silently.
	// Verify it doesn't panic.
	serial.WriteEvent("info", "test message", map[string]any{"key": "value"})
	serial.WriteEvent("error", "error message", nil)
}
