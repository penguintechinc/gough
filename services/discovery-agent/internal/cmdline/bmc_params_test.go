//go:build noxdp

package cmdline_test

import (
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/cmdline"
)

// TestParseStringBMCParams covers the optional gough_bmc and gough_bmc_sha256 keys.
func TestParseStringBMCParams(t *testing.T) {
	base := "gough_token=tok gough_primary=https://api.example.com gough_mac=aa:bb:cc:dd:ee:ff"
	tests := []struct {
		name    string
		extra   string
		wantBMC string
		wantSHA string
	}{
		{name: "no BMC keys", extra: "", wantBMC: "", wantSHA: ""},
		{name: "BMC address only", extra: " gough_bmc=10.0.0.5", wantBMC: "10.0.0.5"},
		{name: "BMC address and pin", extra: " gough_bmc=10.0.0.5 gough_bmc_sha256=abcd", wantBMC: "10.0.0.5", wantSHA: "abcd"},
		{name: "pin key is not mistaken for address", extra: " gough_bmc_sha256=abcd", wantSHA: "abcd"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			p, err := cmdline.ParseString(base + tc.extra)
			if err != nil {
				t.Fatalf("ParseString: %v", err)
			}
			if p.BMC != tc.wantBMC {
				t.Errorf("BMC = %q, want %q", p.BMC, tc.wantBMC)
			}
			if p.BMCSHA256 != tc.wantSHA {
				t.Errorf("BMCSHA256 = %q, want %q", p.BMCSHA256, tc.wantSHA)
			}
		})
	}
}
