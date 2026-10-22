//go:build noxdp

package probe

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/metrics"
)

const bmcConnectTimeout = 10 * time.Second

// ValidateBMCCert connects to the BMC HTTPS endpoint (port 443), retrieves the
// TLS certificate, and computes its SHA256 thumbprint. If expectedThumbprint is
// non-empty and does not match the observed thumbprint, the BMC cert mismatch
// counter is incremented and matched=false is returned. An empty
// expectedThumbprint is treated as first-time registration (matched=true).
func ValidateBMCCert(ctx context.Context, bmcIP, expectedThumbprint string) (matched bool, actualThumbprint string, err error) {
	// Skip verify so we can collect the cert regardless of CA trust.
	transport := &http.Transport{
		TLSClientConfig: &tls.Config{
			InsecureSkipVerify: true, //nolint:gosec // intentional: collecting cert for thumbprint comparison
			MinVersion:         tls.VersionTLS12,
		},
	}
	client := &http.Client{
		Transport: transport,
		Timeout:   bmcConnectTimeout,
	}

	reqCtx, cancel := context.WithTimeout(ctx, bmcConnectTimeout)
	defer cancel()

	url := "https://" + bmcIP
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, url, nil)
	if err != nil {
		return false, "", fmt.Errorf("bmc: build request: %w", err)
	}

	resp, err := client.Do(req)
	if err != nil {
		return false, "", fmt.Errorf("bmc: connect %s: %w", bmcIP, err)
	}
	defer resp.Body.Close()

	if resp.TLS == nil || len(resp.TLS.PeerCertificates) == 0 {
		return false, "", fmt.Errorf("bmc: no TLS certificates from %s", bmcIP)
	}

	// Compute SHA256 over the DER-encoded leaf certificate.
	leaf := resp.TLS.PeerCertificates[0]
	sum := sha256.Sum256(leaf.Raw)
	actualThumbprint = strings.ToLower(hex.EncodeToString(sum[:]))

	if expectedThumbprint == "" {
		return true, actualThumbprint, nil
	}

	if !strings.EqualFold(actualThumbprint, expectedThumbprint) {
		metrics.BMCCertMismatchTotal.Inc()
		return false, actualThumbprint, nil
	}

	return true, actualThumbprint, nil
}
