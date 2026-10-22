//go:build noxdp

package probe_test

import (
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

// TestEnumerateNICs_NoSysfs verifies the function does not panic when the
// sysfs netbase is absent (macOS CI / containers without network namespaces).
func TestEnumerateNICs_NoSysfs(t *testing.T) {
	// The function uses /sys/class/net which is absent on macOS.
	// We just verify it returns an error rather than panicking.
	nics, err := probe.EnumerateNICs()
	if err != nil {
		// Expected on non-Linux environments.
		t.Logf("EnumerateNICs returned error (expected on non-Linux): %v", err)
		return
	}
	// On Linux, verify we got something useful.
	t.Logf("found %d NICs", len(nics))
	for _, n := range nics {
		if n.Name == "" {
			t.Error("NIC name must not be empty")
		}
		if n.Address == "" {
			t.Error("NIC address must not be empty")
		}
	}
}
