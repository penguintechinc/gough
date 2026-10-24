//go:build noxdp

package tags

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

// TestPowerOfTwoCeil covers zero/negative guards and the rounding boundaries.
func TestPowerOfTwoCeil(t *testing.T) {
	tests := []struct {
		in, want int64
	}{
		{in: -5, want: 1},
		{in: 0, want: 1},
		{in: 1, want: 1},
		{in: 2, want: 2},
		{in: 3, want: 4},
		{in: 4, want: 4},
		{in: 5, want: 8},
		{in: 1000, want: 1024},
		{in: 1024, want: 1024},
		{in: 1025, want: 2048},
	}
	for _, tc := range tests {
		if got := powerOfTwoCeil(tc.in); got != tc.want {
			t.Errorf("powerOfTwoCeil(%d) = %d, want %d", tc.in, got, tc.want)
		}
	}
}

// TestSpeedLabel covers every named bucket and the megabit fallback.
func TestSpeedLabel(t *testing.T) {
	tests := []struct {
		mbps int64
		want string
	}{
		{mbps: 400000, want: "400g"},
		{mbps: 200000, want: "200g"},
		{mbps: 100000, want: "100g"},
		{mbps: 40000, want: "40g"},
		{mbps: 25000, want: "25g"},
		{mbps: 10000, want: "10g"},
		{mbps: 1000, want: "1g"},
		{mbps: 100, want: "100m"},
	}
	for _, tc := range tests {
		if got := speedLabel(tc.mbps); got != tc.want {
			t.Errorf("speedLabel(%d) = %q, want %q", tc.mbps, got, tc.want)
		}
	}
}

// TestNICTags covers speed bucketing, vendor classification, and SR-IOV detection.
func TestNICTags(t *testing.T) {
	tests := []struct {
		name     string
		nics     []probe.NICInfo
		wantTags []string
		noTags   []string
	}{
		{
			name:   "no NICs yields no speed tag",
			nics:   nil,
			noTags: []string{"nic:speed:"},
		},
		{
			name:     "highest speed wins and vendor is classified",
			nics:     []probe.NICInfo{{SpeedMb: 10000, PCIID: "15b3:1017"}, {SpeedMb: 25000, PCIID: "8086:1563"}},
			wantTags: []string{"nic:speed:25g", "nic:vendor:mellanox", "nic:vendor:intel-e810"},
		},
		{
			name:     "unknown vendor adds no vendor tag",
			nics:     []probe.NICInfo{{SpeedMb: 1000, PCIID: "dead:beef"}},
			wantTags: []string{"nic:speed:1g"},
			noTags:   []string{"nic:vendor:"},
		},
		{
			name:     "sriov driver flags capability",
			nics:     []probe.NICInfo{{SpeedMb: 10000, Driver: "ixgbe-SRIOV"}},
			wantTags: []string{"nic:sr-iov"},
		},
		{
			name:   "unknown speed below buckets yields no speed tag",
			nics:   []probe.NICInfo{{SpeedMb: 10}},
			noTags: []string{"nic:speed:"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := nicTags(HardwareInput{NICs: tc.nics})
			joined := strings.Join(got, ",")
			for _, w := range tc.wantTags {
				if !strings.Contains(joined, w) {
					t.Errorf("tags %v missing %q", got, w)
				}
			}
			for _, n := range tc.noTags {
				if strings.Contains(joined, n) {
					t.Errorf("tags %v should not contain %q", got, n)
				}
			}
		})
	}
}

// TestReadEFISecureBoot reads the efivar fixture: 4 attribute bytes then the value byte.
func TestReadEFISecureBoot(t *testing.T) {
	tests := []struct {
		name    string
		setup   func(t *testing.T, dir string)
		want    bool
		wantErr bool
	}{
		{
			name: "enabled",
			setup: func(t *testing.T, dir string) {
				writeEFIVar(t, dir, "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c", []byte{6, 0, 0, 0, 1})
			},
			want: true,
		},
		{
			name: "disabled",
			setup: func(t *testing.T, dir string) {
				writeEFIVar(t, dir, "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c", []byte{6, 0, 0, 0, 0})
			},
			want: false,
		},
		{
			name: "short variable rejected",
			setup: func(t *testing.T, dir string) {
				writeEFIVar(t, dir, "SecureBoot-8be4df61", []byte{6, 0})
			},
			wantErr: true,
		},
		{
			name:    "no SecureBoot variable",
			setup:   func(t *testing.T, dir string) { writeEFIVar(t, dir, "PK-8be4df61", []byte{6, 0, 0, 0, 1}) },
			wantErr: true,
		},
		{
			name:    "efivars directory missing",
			setup:   func(*testing.T, string) {},
			wantErr: true,
		},
	}
	orig := efiVarsPath
	t.Cleanup(func() { efiVarsPath = orig })

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			dir := filepath.Join(t.TempDir(), "efivars")
			if tc.name != "efivars directory missing" {
				if err := os.Mkdir(dir, 0o755); err != nil {
					t.Fatal(err)
				}
			}
			tc.setup(t, dir)
			efiVarsPath = dir

			got, err := readEFISecureBoot()
			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, wantErr %v", err, tc.wantErr)
			}
			if got != tc.want {
				t.Errorf("SecureBoot = %v, want %v", got, tc.want)
			}
		})
	}
}

// writeEFIVar writes an efivar fixture file under dir.
func writeEFIVar(t *testing.T, dir, name string, payload []byte) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name), payload, 0o600); err != nil {
		t.Fatal(err)
	}
}
