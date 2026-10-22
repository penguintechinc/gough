//go:build noxdp

package probe

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/metrics"
)

const bmcConnectTimeout = 10 * time.Second

// Environment variables that configure BMC certificate trust.
const (
	envBMCCAFile     = "GOUGH_BMC_CA_FILE"
	envBMCCertSHA256 = "GOUGH_BMC_CERT_SHA256"
)

// Probe status values reported by ValidateBMCCert.
const (
	// BMCStatusVerifiedCA means the chain was verified against the configured CA bundle.
	BMCStatusVerifiedCA = "bmc_verified_ca"
	// BMCStatusVerifiedPin means the leaf matched the configured SHA-256 pin.
	BMCStatusVerifiedPin = "bmc_verified_pin"
	// BMCStatusCertMismatch means the leaf did not match the configured pin.
	BMCStatusCertMismatch = "bmc_cert_mismatch"
	// BMCStatusUnverified means no trust material was configured; no connection was made.
	BMCStatusUnverified = "bmc_tls_unverified"
)

// BMCTrust selects how a BMC's self-signed or private-CA certificate is verified.
// CAFile verifies the chain against a PEM bundle; CertSHA256 pins the leaf
// certificate by the SHA-256 of its DER encoding (lowercase or uppercase hex).
// When both are set, both checks must pass. When neither is set the probe is skipped.
type BMCTrust struct {
	CAFile     string
	CertSHA256 string
}

// BMCResult is the outcome of a BMC TLS probe.
type BMCResult struct {
	// Status is one of the BMCStatus* constants.
	Status string
	// Thumbprint is the lowercase hex SHA-256 of the observed leaf DER; empty when no connection was made.
	Thumbprint string
}

// BMCTrustFromEnv reads GOUGH_BMC_CA_FILE and GOUGH_BMC_CERT_SHA256. A malformed
// pin is an error so a typo fails loudly instead of silently disabling the check.
func BMCTrustFromEnv() (BMCTrust, error) {
	trust := BMCTrust{
		CAFile:     strings.TrimSpace(os.Getenv(envBMCCAFile)),
		CertSHA256: strings.TrimSpace(os.Getenv(envBMCCertSHA256)),
	}
	if trust.CertSHA256 != "" {
		pin, err := normalizePin(trust.CertSHA256)
		if err != nil {
			return BMCTrust{}, err
		}
		trust.CertSHA256 = pin
	}
	return trust, nil
}

// normalizePin validates a 64-character hex SHA-256 and returns it lowercased.
func normalizePin(pin string) (string, error) {
	p := strings.ToLower(strings.TrimSpace(pin))
	if len(p) != sha256.Size*2 {
		return "", fmt.Errorf("bmc: %s must be 64 hex characters, got %d", envBMCCertSHA256, len(p))
	}
	if _, err := hex.DecodeString(p); err != nil {
		return "", fmt.Errorf("bmc: %s is not valid hex: %w", envBMCCertSHA256, err)
	}
	return p, nil
}

// loadCAPool reads a PEM bundle and returns it as a root pool.
func loadCAPool(path string) (*x509.CertPool, error) {
	// The path is operator-configured (GOUGH_BMC_CA_FILE), not network input.
	pem, err := os.ReadFile(path) //#nosec G304 -- operator-supplied CA path from environment
	if err != nil {
		return nil, fmt.Errorf("bmc: read CA %s: %w", path, err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf("bmc: no valid PEM certificates in %s", path)
	}
	return pool, nil
}

// ValidateBMCCert connects to the BMC HTTPS endpoint and verifies its certificate
// according to trust. With no trust configured it returns BMCStatusUnverified
// without opening a connection: an unverified BMC is reported, never trusted.
// A pin mismatch increments the BMC mismatch counter and returns
// BMCStatusCertMismatch with a nil error; transport and configuration failures
// are returned as errors.
func ValidateBMCCert(ctx context.Context, bmcIP string, trust BMCTrust) (BMCResult, error) {
	if trust.CAFile == "" && trust.CertSHA256 == "" {
		return BMCResult{Status: BMCStatusUnverified}, nil
	}

	var pin string
	if trust.CertSHA256 != "" {
		p, err := normalizePin(trust.CertSHA256)
		if err != nil {
			return BMCResult{}, err
		}
		pin = p
	}

	cfg := &tls.Config{MinVersion: tls.VersionTLS12}
	var (
		observed atomic.Pointer[string]
		mismatch atomic.Bool
	)

	if trust.CAFile != "" {
		pool, err := loadCAPool(trust.CAFile)
		if err != nil {
			return BMCResult{}, err
		}
		cfg.RootCAs = pool
	} else {
		// Pin-only path: BMCs typically present self-signed certificates with no chain
		// to verify, so chain verification is skipped. The leaf pin is enforced in
		// VerifyConnection, which runs after every handshake, including resumed ones,
		// so a wrong certificate fails the handshake before any application data.
		cfg.InsecureSkipVerify = true //#nosec G402 -- self-signed BMC; leaf pin enforced in VerifyConnection //nolint:gosec
	}

	if pin != "" {
		// Session tickets are disabled so every connection performs a full handshake
		// and the pin check cannot be bypassed by resumption.
		cfg.SessionTicketsDisabled = true
		cfg.VerifyConnection = func(cs tls.ConnectionState) error {
			if len(cs.PeerCertificates) == 0 {
				return errors.New("bmc: peer presented no certificate")
			}
			sum := sha256.Sum256(cs.PeerCertificates[0].Raw)
			got := hex.EncodeToString(sum[:])
			observed.Store(&got)
			if got != pin {
				mismatch.Store(true)
				return errors.New("bmc: certificate does not match pinned thumbprint")
			}
			return nil
		}
	}

	client := &http.Client{
		Transport: &http.Transport{TLSClientConfig: cfg},
		Timeout:   bmcConnectTimeout,
	}
	reqCtx, cancel := context.WithTimeout(ctx, bmcConnectTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, "https://"+bmcIP, nil)
	if err != nil {
		return BMCResult{}, fmt.Errorf("bmc: build request: %w", err)
	}

	resp, err := client.Do(req)
	if err != nil {
		if mismatch.Load() {
			metrics.BMCCertMismatchTotal.Inc()
			return BMCResult{Status: BMCStatusCertMismatch, Thumbprint: *observed.Load()}, nil
		}
		return BMCResult{}, fmt.Errorf("bmc: connect %s: %w", bmcIP, err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.TLS == nil || len(resp.TLS.PeerCertificates) == 0 {
		return BMCResult{}, fmt.Errorf("bmc: no TLS certificates from %s", bmcIP)
	}
	sum := sha256.Sum256(resp.TLS.PeerCertificates[0].Raw)
	result := BMCResult{Thumbprint: hex.EncodeToString(sum[:])}
	if trust.CAFile != "" {
		result.Status = BMCStatusVerifiedCA
	} else {
		result.Status = BMCStatusVerifiedPin
	}
	return result, nil
}
