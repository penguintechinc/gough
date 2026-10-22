//go:build noxdp

package probe

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/pem"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/metrics"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/testutil"
)

// bmcFixture is a loopback TLS server standing in for a BMC.
type bmcFixture struct {
	addr   string // host:port without scheme
	sha    string // lowercase hex SHA-256 of the leaf DER
	caPath string // PEM file containing the server's leaf certificate
	conns  *atomic.Int32
}

// newBMC starts a TLS server, counts accepted TCP connections, and writes its leaf to a CA file.
func newBMC(t *testing.T) bmcFixture {
	t.Helper()
	conns := &atomic.Int32{}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	srv.Config.ConnState = func(_ net.Conn, s http.ConnState) {
		if s == http.StateNew {
			conns.Add(1)
		}
	}
	srv.StartTLS()
	t.Cleanup(srv.Close)

	leaf := srv.Certificate()
	sum := sha256.Sum256(leaf.Raw)
	caPath := filepath.Join(t.TempDir(), "bmc-ca.pem")
	if err := os.WriteFile(caPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: leaf.Raw}), 0o600); err != nil {
		t.Fatal(err)
	}
	return bmcFixture{
		addr:   strings.TrimPrefix(srv.URL, "https://"),
		sha:    hex.EncodeToString(sum[:]),
		caPath: caPath,
		conns:  conns,
	}
}

// mismatchCount reads the current value of the BMC mismatch counter from the default gatherer.
func mismatchCount(t *testing.T) float64 {
	t.Helper()
	families, err := prometheus.DefaultGatherer.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() == "gough_bmc_certificate_mismatch_detected_total" && len(f.GetMetric()) > 0 {
			return f.GetMetric()[0].GetCounter().GetValue()
		}
	}
	t.Fatal("bmc mismatch counter not exposed")
	return 0
}

func init() {
	metrics.Register()
}

// TestValidateBMCCert covers every trust mode against a live TLS server.
func TestValidateBMCCert(t *testing.T) {
	missingCA := filepath.Join(t.TempDir(), "absent.pem")
	garbageCA := filepath.Join(t.TempDir(), "garbage.pem")
	if err := os.WriteFile(garbageCA, []byte("not pem"), 0o600); err != nil {
		t.Fatal(err)
	}
	otherCA := testutil.WriteCAFile(t, testutil.NewSelfSignedCert(t))
	wrongPin := strings.Repeat("aa", 32)

	tests := []struct {
		name         string
		trust        func(f bmcFixture) BMCTrust
		addr         func(f bmcFixture) string
		wantStatus   string
		wantErr      bool
		wantConns    int32
		wantMismatch bool
	}{
		{
			name:       "nothing configured never connects",
			trust:      func(bmcFixture) BMCTrust { return BMCTrust{} },
			wantStatus: BMCStatusUnverified,
			wantConns:  0,
		},
		{
			name:       "pin matches",
			trust:      func(f bmcFixture) BMCTrust { return BMCTrust{CertSHA256: f.sha} },
			wantStatus: BMCStatusVerifiedPin,
			wantConns:  1,
		},
		{
			name:       "pin matches with uppercase hex",
			trust:      func(f bmcFixture) BMCTrust { return BMCTrust{CertSHA256: strings.ToUpper(f.sha)} },
			wantStatus: BMCStatusVerifiedPin,
			wantConns:  1,
		},
		{
			name:         "wrong pin reports mismatch and increments counter",
			trust:        func(bmcFixture) BMCTrust { return BMCTrust{CertSHA256: wrongPin} },
			wantStatus:   BMCStatusCertMismatch,
			wantConns:    1,
			wantMismatch: true,
		},
		{
			name:       "CA bundle verifies chain",
			trust:      func(f bmcFixture) BMCTrust { return BMCTrust{CAFile: f.caPath} },
			wantStatus: BMCStatusVerifiedCA,
			wantConns:  1,
		},
		{
			name:       "CA plus matching pin",
			trust:      func(f bmcFixture) BMCTrust { return BMCTrust{CAFile: f.caPath, CertSHA256: f.sha} },
			wantStatus: BMCStatusVerifiedCA,
			wantConns:  1,
		},
		{
			name:         "CA plus wrong pin reports mismatch",
			trust:        func(f bmcFixture) BMCTrust { return BMCTrust{CAFile: f.caPath, CertSHA256: wrongPin} },
			wantStatus:   BMCStatusCertMismatch,
			wantConns:    1,
			wantMismatch: true,
		},
		{
			name:      "CA that did not issue the leaf fails the handshake",
			trust:     func(bmcFixture) BMCTrust { return BMCTrust{CAFile: otherCA} },
			wantErr:   true,
			wantConns: 1,
		},
		{
			name:      "missing CA file",
			trust:     func(bmcFixture) BMCTrust { return BMCTrust{CAFile: missingCA} },
			wantErr:   true,
			wantConns: 0,
		},
		{
			name:      "CA file without PEM certificates",
			trust:     func(bmcFixture) BMCTrust { return BMCTrust{CAFile: garbageCA} },
			wantErr:   true,
			wantConns: 0,
		},
		{
			name:      "malformed pin rejected before connecting",
			trust:     func(bmcFixture) BMCTrust { return BMCTrust{CertSHA256: "abc123"} },
			wantErr:   true,
			wantConns: 0,
		},
		{
			name:      "non-hex pin rejected before connecting",
			trust:     func(bmcFixture) BMCTrust { return BMCTrust{CertSHA256: strings.Repeat("zz", 32)} },
			wantErr:   true,
			wantConns: 0,
		},
		{
			name:      "unreachable BMC is an error, not a mismatch",
			trust:     func(f bmcFixture) BMCTrust { return BMCTrust{CertSHA256: f.sha} },
			addr:      func(bmcFixture) string { return "127.0.0.1:1" },
			wantErr:   true,
			wantConns: 0,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			f := newBMC(t)
			addr := f.addr
			if tc.addr != nil {
				addr = tc.addr(f)
			}
			before := mismatchCount(t)

			res, err := ValidateBMCCert(context.Background(), addr, tc.trust(f))

			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, wantErr %v", err, tc.wantErr)
			}
			if got := f.conns.Load(); got != tc.wantConns {
				t.Errorf("connections = %d, want %d", got, tc.wantConns)
			}
			if tc.wantErr {
				return
			}
			if res.Status != tc.wantStatus {
				t.Errorf("Status = %q, want %q", res.Status, tc.wantStatus)
			}
			if tc.wantStatus == BMCStatusUnverified {
				if res.Thumbprint != "" {
					t.Errorf("Thumbprint = %q, want empty when unverified", res.Thumbprint)
				}
				return
			}
			if res.Thumbprint != f.sha {
				t.Errorf("Thumbprint = %q, want observed leaf %q", res.Thumbprint, f.sha)
			}
			delta := mismatchCount(t) - before
			if tc.wantMismatch && delta != 1 {
				t.Errorf("mismatch counter delta = %v, want 1", delta)
			}
			if !tc.wantMismatch && delta != 0 {
				t.Errorf("mismatch counter delta = %v, want 0", delta)
			}
		})
	}
}

// TestBMCTrustFromEnv reads and validates the two environment variables.
func TestBMCTrustFromEnv(t *testing.T) {
	valid := strings.Repeat("AB", 32)
	tests := []struct {
		name    string
		ca      string
		pin     string
		want    BMCTrust
		wantErr bool
	}{
		{name: "unset yields empty trust", want: BMCTrust{}},
		{name: "CA file only", ca: "/etc/gough/bmc-ca.pem", want: BMCTrust{CAFile: "/etc/gough/bmc-ca.pem"}},
		{name: "pin normalised to lowercase", pin: valid, want: BMCTrust{CertSHA256: strings.ToLower(valid)}},
		{name: "pin too short", pin: "abcd", wantErr: true},
		{name: "pin not hex", pin: strings.Repeat("g", 64), wantErr: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(envBMCCAFile, tc.ca)
			t.Setenv(envBMCCertSHA256, tc.pin)
			got, err := BMCTrustFromEnv()
			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, wantErr %v", err, tc.wantErr)
			}
			if !tc.wantErr && got != tc.want {
				t.Errorf("trust = %+v, want %+v", got, tc.want)
			}
		})
	}
}

// TestNormalizePin covers length and hex validation directly.
func TestNormalizePin(t *testing.T) {
	tests := []struct {
		name    string
		in      string
		want    string
		wantErr bool
	}{
		{name: "valid mixed case", in: "  " + strings.Repeat("Ab", 32) + " ", want: strings.Repeat("ab", 32)},
		{name: "empty", in: "", wantErr: true},
		{name: "odd length", in: strings.Repeat("a", 63), wantErr: true},
		{name: "non hex", in: strings.Repeat("x", 64), wantErr: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := normalizePin(tc.in)
			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, wantErr %v", err, tc.wantErr)
			}
			if got != tc.want {
				t.Errorf("normalizePin = %q, want %q", got, tc.want)
			}
		})
	}
}
