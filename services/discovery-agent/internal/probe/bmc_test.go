//go:build noxdp

package probe_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

// startTLSServer starts a local TLS test server and returns the host:port and
// the SHA256 thumbprint of its self-signed certificate.
func startTLSServer(t *testing.T) (hostPort, thumbprint string) {
	t.Helper()
	srv := httptest.NewTLSServer(nil)
	t.Cleanup(srv.Close)

	// Extract host:port without the scheme.
	addr := strings.TrimPrefix(srv.URL, "https://")

	// Compute thumbprint from the server's leaf certificate.
	certs := srv.TLS.Certificates
	if len(certs) == 0 || len(certs[0].Certificate) == 0 {
		t.Fatal("test server has no certificate")
	}
	sum := sha256.Sum256(certs[0].Certificate[0])
	fp := strings.ToLower(hex.EncodeToString(sum[:]))
	return addr, fp
}

func TestValidateBMCCert_MatchingThumbprint(t *testing.T) {
	addr, expected := startTLSServer(t)

	matched, actual, err := probe.ValidateBMCCert(context.Background(), addr, expected)
	if err != nil {
		t.Fatalf("ValidateBMCCert: %v", err)
	}
	if !matched {
		t.Errorf("matched = false, want true (actual=%s, expected=%s)", actual, expected)
	}
	if actual != expected {
		t.Errorf("actual thumbprint %s != expected %s", actual, expected)
	}
}

func TestValidateBMCCert_MismatchThumbprint(t *testing.T) {
	addr, _ := startTLSServer(t)
	wrong := strings.Repeat("aa", 32) // 64-char hex, wrong value

	matched, actual, err := probe.ValidateBMCCert(context.Background(), addr, wrong)
	if err != nil {
		t.Fatalf("ValidateBMCCert: %v", err)
	}
	if matched {
		t.Error("matched = true, want false for wrong thumbprint")
	}
	if actual == "" {
		t.Error("expected non-empty actual thumbprint")
	}
}

func TestValidateBMCCert_EmptyExpected_FirstRegistration(t *testing.T) {
	addr, _ := startTLSServer(t)

	matched, actual, err := probe.ValidateBMCCert(context.Background(), addr, "")
	if err != nil {
		t.Fatalf("ValidateBMCCert: %v", err)
	}
	if !matched {
		t.Error("matched = false, want true for first-time registration (empty expected)")
	}
	if actual == "" {
		t.Error("expected non-empty actual thumbprint on first registration")
	}
}

func TestValidateBMCCert_UnreachableHost(t *testing.T) {
	// Use a non-routable address to simulate unreachable BMC.
	_, _, err := probe.ValidateBMCCert(context.Background(), "192.0.2.1:443", "")
	if err == nil {
		t.Error("expected error for unreachable BMC host")
	}
}

func TestSmartStatusString(t *testing.T) {
	tests := []struct {
		passed bool
		want   string
	}{
		{true, "healthy"},
		{false, "failed"},
	}
	for _, tt := range tests {
		got := probe.SmartStatusString(tt.passed)
		if got != tt.want {
			t.Errorf("SmartStatusString(%v) = %q, want %q", tt.passed, got, tt.want)
		}
	}
}
