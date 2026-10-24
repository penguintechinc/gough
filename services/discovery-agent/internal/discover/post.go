//go:build noxdp

// Package discover handles the POST /api/v1/nodes/discover HTTP call.
package discover

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/metrics"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

const (
	discoverPath = "/api/v1/nodes/discover"
	httpTimeout  = 30 * time.Second
)

// Request is the JSON body sent to POST /api/v1/nodes/discover. Its shape
// mirrors the api-manager DiscoverRequest schema (services/api-manager/app/api/_schemas/nodes.py);
// the golden fixture in testdata/ and the hub contract test pin it.
type Request struct {
	// DMIUUID is the DMI system UUID (api-manager dmi_uuid, required).
	DMIUUID string `json:"dmi_uuid"`
	// PrimaryNICMAC is the MAC of the provisioning NIC (required).
	PrimaryNICMAC string `json:"primary_nic_mac"`
	// FirmwareType is "bios" or "uefi".
	FirmwareType string `json:"firmware_type"`
	// Hostname is the kernel hostname at probe time; optional.
	Hostname string `json:"hostname,omitempty"`
	// SysVendor, ProductName and BIOSVersion are DMI strings; optional.
	SysVendor   string `json:"sys_vendor,omitempty"`
	ProductName string `json:"product_name,omitempty"`
	BIOSVersion string `json:"bios_version,omitempty"`
	// LshwJSON is the lshw -json object forwarded verbatim; omitted when lshw failed.
	LshwJSON json.RawMessage `json:"lshw_json,omitempty"`
	// LsblkJSON is the lsblk --json object forwarded verbatim; omitted when absent.
	LsblkJSON json.RawMessage `json:"lsblk_json,omitempty"`
	// SmartAttributes is the flattened ATA SMART table across all disks.
	SmartAttributes []SmartAttribute `json:"smart_attributes"`
	// Nics lists enumerated NICs; the provisioning NIC has IsPrimary set.
	Nics []NicInfo `json:"nics"`
	// NumaTopology is omitted when numactl reported no NUMA nodes.
	NumaTopology *NumaTopology `json:"numa_topology,omitempty"`
	// Accelerators lists PCI GPU/accelerator devices.
	Accelerators []AcceleratorInfo `json:"accelerators"`
	// HardwareTags is the auto-discovered tag set.
	HardwareTags []string `json:"hardware_tags"`
	// BMCTLSStatus is the BMC certificate verification outcome; omitted when the BMC was not probed.
	BMCTLSStatus string `json:"bmc_tls_status,omitempty"`
	// BMCCertSHA256 is the observed BMC leaf-certificate SHA-256 (lowercase hex); empty when no connection was made.
	BMCCertSHA256 string `json:"bmc_cert_sha256,omitempty"`
}

// SmartAttribute is one ATA SMART table row, tagged with its disk.
type SmartAttribute struct {
	Device   string `json:"device,omitempty"`
	ID       int    `json:"id"`
	Name     string `json:"name"`
	Value    int    `json:"value"`
	Worst    int    `json:"worst"`
	Thresh   int    `json:"thresh"`
	RawValue *int64 `json:"raw_value,omitempty"`
}

// NicInfo describes one enumerated network interface.
type NicInfo struct {
	MAC       string `json:"mac"`
	SpeedMbps *int64 `json:"speed_mbps,omitempty"`
	PCISlot   string `json:"pci_slot,omitempty"`
	IsPrimary bool   `json:"is_primary"`
	Name      string `json:"name,omitempty"`
}

// NumaTopology is the NUMA layout keyed by node id as a decimal string.
type NumaTopology struct {
	Nodes    int              `json:"nodes"`
	CPUMap   map[string][]int `json:"cpu_map"`
	MemoryMB map[string]int64 `json:"memory_mb"`
}

// AcceleratorInfo describes one PCI GPU or accelerator.
type AcceleratorInfo struct {
	BusType    string `json:"bus_type"`
	Vendor     string `json:"vendor"`
	Model      string `json:"model"`
	VRAMMB     *int64 `json:"vram_mb,omitempty"`
	PCIAddress string `json:"pci_address,omitempty"`
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

// UnmarshalJSON decodes the hub's response, which wraps the body in a
// {"status","data","meta"} envelope and returns node_id as a number. A bare
// (unwrapped) body with a string node_id is still accepted.
func (r *Response) UnmarshalJSON(b []byte) error {
	var env struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(b, &env); err != nil {
		return err
	}
	if len(env.Data) > 0 && string(env.Data) != "null" {
		b = env.Data
	}
	var aux struct {
		NodeID                json.RawMessage `json:"node_id"`
		SpireJoinToken        string          `json:"spire_join_token"`
		ControlTunnelEndpoint string          `json:"control_tunnel_endpoint"`
		ControlTunnelSpiffeID string          `json:"control_tunnel_spiffe_id"`
	}
	if err := json.Unmarshal(b, &aux); err != nil {
		return err
	}
	id := strings.Trim(string(aux.NodeID), `"`)
	if id == "null" {
		id = ""
	}
	*r = Response{
		NodeID:                id,
		SpireJoinToken:        aux.SpireJoinToken,
		ControlTunnelEndpoint: aux.ControlTunnelEndpoint,
		ControlTunnelSpiffeID: aux.ControlTunnelSpiffeID,
	}
	return nil
}

// StatusError reports a non-2xx response from the discover endpoint.
type StatusError struct {
	// Code is the HTTP status code returned by the api-manager.
	Code int
	// URL is the full endpoint that was called.
	URL string
}

// Error implements error.
func (e *StatusError) Error() string {
	return fmt.Sprintf("discover: unexpected status %d from %s", e.Code, e.URL)
}

// IsPermanent reports whether retrying the POST cannot succeed. 401 and 403
// mean the bootstrap token was rejected; retrying only hammers the api-manager.
func IsPermanent(err error) bool {
	var se *StatusError
	if !errors.As(err, &se) {
		return false
	}
	return se.Code == http.StatusUnauthorized || se.Code == http.StatusForbidden
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
	// Each call owns its transport; release idle connections on return so
	// retries do not accumulate sockets.
	defer client.CloseIdleConnections()

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
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusCreated && resp.StatusCode != http.StatusOK {
		return Response{}, &StatusError{Code: resp.StatusCode, URL: url}
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
