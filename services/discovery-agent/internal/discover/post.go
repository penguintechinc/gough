//go:build noxdp

// Package discover handles the POST /api/v1/nodes/discover HTTP call.
package discover

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/metrics"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

const (
	discoverPath  = "/api/v1/nodes/discover"
	httpTimeout   = 30 * time.Second
)

// Request is the JSON body sent to POST /api/v1/nodes/discover.
type Request struct {
	// MACAddress is the MAC of the primary boot interface.
	MACAddress string `json:"mac_address"`
	// Hostname is the current kernel hostname (may be "localhost" in helper image).
	Hostname string `json:"hostname"`
	// DMI fields.
	ProductUUID string `json:"product_uuid"`
	SysVendor   string `json:"sys_vendor"`
	ProductName string `json:"product_name"`
	BIOSVersion string `json:"bios_version"`
	// Firmware type ("uefi" or "bios").
	FirmwareType string `json:"firmware_type"`
	// Lshw is the raw lshw -json output forwarded verbatim.
	Lshw json.RawMessage `json:"lshw,omitempty"`
	// Lsblk is the raw lsblk --json -O output forwarded verbatim.
	Lsblk json.RawMessage `json:"lsblk,omitempty"`
	// SMART holds per-disk smartctl -a -j output.
	SMART []json.RawMessage `json:"smart,omitempty"`
	// HardwareTags is the auto-discovered tag set.
	HardwareTags []string `json:"hardware_tags"`
	// NumaJSON is the raw lstopo output if available.
	NumaJSON json.RawMessage `json:"numa,omitempty"`
	// AcceleratorJSON is the serialised accelerator probe.
	AcceleratorJSON json.RawMessage `json:"accelerators,omitempty"`
}

// Response is the JSON body returned by POST /api/v1/nodes/discover on success.
type Response struct {
	// NodeID is the UUID assigned to this node in the Gough database.
	NodeID string `json:"node_id"`
	// SpireJoinToken is the one-time SPIRE join token for fetching an SVID.
	SpireJoinToken string `json:"spire_join_token"`
	// ControlTunnelEndpoint is "host:8443" to open the gRPC mTLS tunnel.
	ControlTunnelEndpoint string `json:"control_tunnel_endpoint"`
	// ControlTunnelSpiffeID is the expected SPIFFE ID of the api-manager.
	ControlTunnelSpiffeID string `json:"control_tunnel_spiffe_id"`
}

// SmartPreflight checks all SMART results and returns an error if any disk
// reports a failed status. It also increments the failure counter per failed disk.
func SmartPreflight(smartResults []probe.SmartOutput) error {
	for _, s := range smartResults {
		if probe.SmartStatusString(s.SmartStatus.Passed) == "failed" {
			metrics.SmartFailureTotal.Inc()
			return fmt.Errorf("discover: SMART preflight failed for disk %s (serial %s): status=failed",
				s.Device.Name, s.SerialNumber)
		}
	}
	return nil
}

// Post sends the discovery payload to primaryURL and returns the api-manager
// response.  jwt is the one-time bootstrap token from the kernel cmdline.
// caPEMPath is the path to the internal CA certificate in the initrd.
func Post(ctx context.Context, primaryURL, jwt, caPEMPath string, req Request) (Response, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return Response{}, fmt.Errorf("discover: marshal: %w", err)
	}

	tlsCfg, err := buildTLSConfig(caPEMPath)
	if err != nil {
		return Response{}, fmt.Errorf("discover: tls config: %w", err)
	}

	client := &http.Client{
		Timeout:   httpTimeout,
		Transport: &http.Transport{TLSClientConfig: tlsCfg},
	}

	url := primaryURL + discoverPath
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return Response{}, fmt.Errorf("discover: new request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+jwt)

	resp, err := client.Do(httpReq)
	if err != nil {
		return Response{}, fmt.Errorf("discover: post %s: %w", url, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusCreated && resp.StatusCode != http.StatusOK {
		return Response{}, fmt.Errorf("discover: unexpected status %d from %s", resp.StatusCode, url)
	}

	var result Response
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return Response{}, fmt.Errorf("discover: decode response: %w", err)
	}
	return result, nil
}

// buildTLSConfig loads the CA certificate from caPEMPath and returns a
// TLS config that validates the api-manager's certificate against it.
func buildTLSConfig(caPEMPath string) (*tls.Config, error) {
	if caPEMPath == "" {
		// No CA pinning — use system roots.  Should not happen in production.
		return &tls.Config{MinVersion: tls.VersionTLS12}, nil
	}

	caPEM, err := os.ReadFile(caPEMPath)
	if err != nil {
		return nil, fmt.Errorf("read CA %s: %w", caPEMPath, err)
	}

	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("no valid PEM certificates in %s", caPEMPath)
	}

	return &tls.Config{
		RootCAs:    pool,
		MinVersion: tls.VersionTLS12,
	}, nil
}
