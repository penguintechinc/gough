//go:build noxdp

package testutil

import (
	"bytes"
	"crypto/x509"
	"encoding/pem"
	"net"
	"os"
	"testing"
)

// TestNewSelfSignedCert checks the generated certificate is a usable trust anchor for loopback.
func TestNewSelfSignedCert(t *testing.T) {
	tests := []struct {
		name   string
		host   string
		wantOK bool
	}{
		{name: "loopback IPv4 verifies", host: "127.0.0.1", wantOK: true},
		{name: "loopback IPv6 verifies", host: "::1", wantOK: true},
		{name: "localhost verifies", host: "localhost", wantOK: true},
		{name: "unrelated host rejected", host: "example.org", wantOK: false},
	}

	c := NewSelfSignedCert(t)

	block, _ := pem.Decode(c.CAPEM)
	if block == nil || block.Type != "CERTIFICATE" {
		t.Fatalf("CAPEM is not a PEM CERTIFICATE block")
	}
	if !bytes.Equal(block.Bytes, c.DER) {
		t.Fatalf("CAPEM DER does not match DER field")
	}
	leaf, err := x509.ParseCertificate(c.DER)
	if err != nil {
		t.Fatalf("parse leaf: %v", err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(leaf)

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			opts := x509.VerifyOptions{Roots: pool, DNSName: tc.host}
			if ip := net.ParseIP(tc.host); ip != nil {
				opts.DNSName = ""
				// Verify IP SANs explicitly: VerifyOptions has no IP field.
				found := false
				for _, s := range leaf.IPAddresses {
					if s.Equal(ip) {
						found = true
					}
				}
				if found != tc.wantOK {
					t.Errorf("IP %s SAN present = %v, want %v", tc.host, found, tc.wantOK)
				}
				return
			}
			_, err := leaf.Verify(opts)
			if (err == nil) != tc.wantOK {
				t.Errorf("verify %s err = %v, wantOK %v", tc.host, err, tc.wantOK)
			}
		})
	}
}

// TestWriteCAFile checks the CA file on disk is byte-identical to CAPEM.
func TestWriteCAFile(t *testing.T) {
	c := NewSelfSignedCert(t)
	path := WriteCAFile(t, c)
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if !bytes.Equal(got, c.CAPEM) {
		t.Errorf("CA file contents differ from CAPEM")
	}
}

// TestNewSVID checks the SVID carries exactly the requested SPIFFE ID and a parseable PKCS#8 key.
func TestNewSVID(t *testing.T) {
	tests := []struct {
		name string
		id   string
	}{
		{name: "agent id", id: "spiffe://penguintech.io/beta/discovery-agent"},
		{name: "nested path", id: "spiffe://penguintech.io/gamma/api-manager"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			s := NewSVID(t, tc.id)
			leaf, err := x509.ParseCertificate(s.DER)
			if err != nil {
				t.Fatalf("parse SVID: %v", err)
			}
			if len(leaf.URIs) != 1 || leaf.URIs[0].String() != tc.id {
				t.Errorf("URI SANs = %v, want exactly %s", leaf.URIs, tc.id)
			}
			if _, err := x509.ParsePKCS8PrivateKey(s.KeyPKCS8); err != nil {
				t.Errorf("parse PKCS8 key: %v", err)
			}
		})
	}
}
