//go:build noxdp

// Package testutil provides test-only helpers shared across discovery-agent
// packages. Nothing in this package is linked into the production binary.
package testutil

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Cert bundles a self-signed server certificate with its PEM-encoded CA form.
type Cert struct {
	// TLS is the key pair usable as tls.Config.Certificates[0].
	TLS tls.Certificate
	// CAPEM is the PEM-encoded certificate, usable as a trust anchor file.
	CAPEM []byte
	// DER is the raw leaf certificate, useful for SVID bundle payloads.
	DER []byte
	// Key is the ECDSA private key backing TLS.
	Key *ecdsa.PrivateKey
}

// NewSelfSignedCert returns a self-signed ECDSA P-256 certificate valid for
// 127.0.0.1, ::1 and localhost. It is a CA as well as a leaf so the same
// PEM serves as the trust anchor for client-side verification.
func NewSelfSignedCert(tb testing.TB) Cert {
	tb.Helper()

	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		tb.Fatalf("generate key: %v", err)
	}

	serial, err := rand.Int(rand.Reader, big.NewInt(1<<62))
	if err != nil {
		tb.Fatalf("serial: %v", err)
	}

	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: "discovery-agent-test"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")},
		DNSNames:              []string{"localhost"},
	}

	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		tb.Fatalf("create certificate: %v", err)
	}

	return Cert{
		TLS: tls.Certificate{
			Certificate: [][]byte{der},
			PrivateKey:  key,
		},
		CAPEM: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		DER:   der,
		Key:   key,
	}
}

// SVID is an X.509 SVID fixture: a leaf certificate carrying a SPIFFE URI SAN.
type SVID struct {
	// DER is the leaf certificate, also usable as its own trust bundle.
	DER []byte
	// KeyPKCS8 is the unencrypted PKCS#8 private key matching DER.
	KeyPKCS8 []byte
}

// NewSVID mints a self-signed X.509 SVID whose only URI SAN is spiffeID.
// Client-side verification is not exercised; the SVID only needs to parse.
func NewSVID(tb testing.TB, spiffeID string) SVID {
	tb.Helper()
	id, err := url.Parse(spiffeID)
	if err != nil {
		tb.Fatalf("parse spiffe id: %v", err)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		tb.Fatalf("generate key: %v", err)
	}
	serial, err := rand.Int(rand.Reader, big.NewInt(1<<62))
	if err != nil {
		tb.Fatalf("serial: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: "svid-test"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		URIs:         []*url.URL{id},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		tb.Fatalf("create svid: %v", err)
	}
	pkcs8, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		tb.Fatalf("marshal key: %v", err)
	}
	return SVID{DER: der, KeyPKCS8: pkcs8}
}

// WriteCAFile writes the CA PEM to a temp file inside tb.TempDir and returns its path.
func WriteCAFile(tb testing.TB, c Cert) string {
	tb.Helper()
	path := filepath.Join(tb.TempDir(), "ca.pem")
	if err := os.WriteFile(path, c.CAPEM, 0o600); err != nil {
		tb.Fatalf("write CA file: %v", err)
	}
	return path
}
