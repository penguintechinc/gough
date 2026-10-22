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
	"net"
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

// Validate checks the trust configuration without touching the network: the pin
// must be a well-formed SHA-256 and the CA bundle, if set, must load. A failure
// here is a configuration error and should stop the agent rather than be reported as
// an unreachable BMC.
func (t BMCTrust) Validate() error {
	if t.CertSHA256 != "" {
		if _, err := normalizePin(t.CertSHA256); err != nil {
			return err
		}
	}
	if t.CAFile != "" {
		if _, err := loadCAPool(t.CAFile); err != nil {
			return err
		}
	}
	return nil
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
// Certificate verification failures (unknown authority, invalid chain, hostname
// mismatch, pin mismatch) return BMCStatusCertMismatch with the observed leaf
// SHA-256 and a nil error; only dial, timeout and configuration failures are
// returned as errors (reported upstream as bmc_unreachable).
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

	var roots *x509.CertPool
	if trust.CAFile != "" {
		pool, err := loadCAPool(trust.CAFile)
		if err != nil {
			return BMCResult{}, err
		}
		roots = pool
	}

	// Verification runs in VerifyConnection so the observed leaf is captured even
	// when the chain is rejected. InsecureSkipVerify only disables the stdlib
	// chain check; the explicit chain and pin checks below replace it.
	cfg := &tls.Config{
		MinVersion:             tls.VersionTLS12,
		InsecureSkipVerify:     true, //#nosec G402 -- replaced by VerifyConnection chain/pin checks below //nolint:gosec
		SessionTicketsDisabled: true, // full handshake on every connection so checks cannot be resumed around
	}
	var (
		observed atomic.Pointer[string]
		mismatch atomic.Bool
	)
	cfg.VerifyConnection = func(cs tls.ConnectionState) error {
		if len(cs.PeerCertificates) == 0 {
			return errors.New("bmc: peer presented no certificate")
		}
		leaf := cs.PeerCertificates[0]
		sum := sha256.Sum256(leaf.Raw)
		got := hex.EncodeToString(sum[:])
		observed.Store(&got)

		if roots != nil {
			inter := x509.NewCertPool()
			for _, c := range cs.PeerCertificates[1:] {
				inter.AddCert(c)
			}
			_, err := leaf.Verify(x509.VerifyOptions{
				DNSName:       bmcHost(bmcIP),
				Roots:         roots,
				Intermediates: inter,
				KeyUsages:     []x509.ExtKeyUsage{x509.ExtKeyUsageAny},
			})
			if err != nil {
				mismatch.Store(true)
				return fmt.Errorf("bmc: chain verification failed: %w", err)
			}
		}
		if pin != "" && got != pin {
			mismatch.Store(true)
			return errors.New("bmc: certificate does not match pinned thumbprint")
		}
		return nil
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
		if mismatch.Load() || isVerificationError(err) {
			metrics.BMCCertMismatchTotal.Inc()
			thumb := ""
			if p := observed.Load(); p != nil {
				thumb = *p
			}
			return BMCResult{Status: BMCStatusCertMismatch, Thumbprint: thumb}, nil
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

// bmcHost strips an optional :port so the certificate is checked against the host alone.
func bmcHost(addr string) string {
	if host, _, err := net.SplitHostPort(addr); err == nil {
		return host
	}
	return addr
}

// isVerificationError reports whether err originates from X.509 or TLS
// certificate verification, as opposed to a dial, timeout or protocol failure.
func isVerificationError(err error) bool {
	var (
		unknownAuth x509.UnknownAuthorityError
		invalid     x509.CertificateInvalidError
		hostErr     x509.HostnameError
		certVerify  *tls.CertificateVerificationError
	)
	return errors.As(err, &unknownAuth) ||
		errors.As(err, &invalid) ||
		errors.As(err, &hostErr) ||
		errors.As(err, &certVerify)
}
