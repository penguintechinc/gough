//go:build noxdp

package serial_test

import (
	"testing"
	"time"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/serial"
)

// TestShouldUseFallback covers the zero value, the grace period, and elapsed failures.
func TestShouldUseFallback(t *testing.T) {
	tests := []struct {
		name     string
		failedAt time.Time
		want     bool
	}{
		{name: "never failed", failedAt: time.Time{}, want: false},
		{name: "failed just now", failedAt: time.Now(), want: false},
		{name: "failed 4m ago still in grace", failedAt: time.Now().Add(-4 * time.Minute), want: false},
		{name: "failed 10m ago", failedAt: time.Now().Add(-10 * time.Minute), want: true},
		{name: "failed exactly 5m ago", failedAt: time.Now().Add(-5 * time.Minute), want: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := serial.ShouldUseFallback(tc.failedAt); got != tc.want {
				t.Errorf("ShouldUseFallback = %v, want %v", got, tc.want)
			}
		})
	}
}
