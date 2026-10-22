//go:build noxdp

package discover

import (
	"crypto/tls"
	"os"
	"path/filepath"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/testutil"
)

// TestBuildTLSConfig verifies CA pinning and the TLS 1.2 floor for each CA path shape.
func TestBuildTLSConfig(t *testing.T) {
	cert := testutil.NewSelfSignedCert(t)
	garbage := filepath.Join(t.TempDir(), "garbage.pem")
	if err := os.WriteFile(garbage, []byte("not a certificate"), 0o600); err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name       string
		path       func(t *testing.T) string
		wantErr    bool
		wantPinned bool
	}{
		{name: "empty path uses system roots", path: func(*testing.T) string { return "" }},
		{name: "valid CA is pinned", path: func(t *testing.T) string { return testutil.WriteCAFile(t, cert) }, wantPinned: true},
		{name: "missing file", path: func(t *testing.T) string { return filepath.Join(t.TempDir(), "nope.pem") }, wantErr: true},
		{name: "no PEM blocks", path: func(*testing.T) string { return garbage }, wantErr: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cfg, err := buildTLSConfig(tc.path(t))
			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, wantErr %v", err, tc.wantErr)
			}
			if tc.wantErr {
				return
			}
			if cfg.MinVersion != tls.VersionTLS12 {
				t.Errorf("MinVersion = %x, want TLS 1.2", cfg.MinVersion)
			}
			if (cfg.RootCAs != nil) != tc.wantPinned {
				t.Errorf("RootCAs pinned = %v, want %v", cfg.RootCAs != nil, tc.wantPinned)
			}
		})
	}
}
