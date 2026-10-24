//go:build noxdp

package probe

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/testutil"
)

// TestBMCTrustValidate checks configuration errors are caught without any network access.
func TestBMCTrustValidate(t *testing.T) {
	goodCA := testutil.WriteCAFile(t, testutil.NewSelfSignedCert(t))
	garbage := filepath.Join(t.TempDir(), "garbage.pem")
	if err := os.WriteFile(garbage, []byte("not pem"), 0o600); err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name    string
		trust   BMCTrust
		wantErr bool
	}{
		{name: "empty trust is valid (probe reports unverified)", trust: BMCTrust{}},
		{name: "well-formed pin", trust: BMCTrust{CertSHA256: strings.Repeat("ab", 32)}},
		{name: "malformed pin", trust: BMCTrust{CertSHA256: "short"}, wantErr: true},
		{name: "readable CA bundle", trust: BMCTrust{CAFile: goodCA}},
		{name: "missing CA bundle", trust: BMCTrust{CAFile: filepath.Join(t.TempDir(), "none.pem")}, wantErr: true},
		{name: "CA bundle without certificates", trust: BMCTrust{CAFile: garbage}, wantErr: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.trust.Validate()
			if (err != nil) != tc.wantErr {
				t.Fatalf("Validate error = %v, wantErr %v", err, tc.wantErr)
			}
		})
	}
}
