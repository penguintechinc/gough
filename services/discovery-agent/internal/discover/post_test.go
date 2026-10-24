//go:build noxdp

package discover_test

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/discover"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/metrics"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/testutil"
)

func init() {
	metrics.Register()
}

// smartWith builds a SmartOutput with the given device name, serial and health.
func smartWith(name, serial string, passed bool) probe.SmartOutput {
	var s probe.SmartOutput
	s.Device.Name = name
	s.SerialNumber = serial
	s.SmartStatus.Passed = passed
	return s
}

func TestPost(t *testing.T) {
	okResp := discover.Response{
		NodeID:                "node-uuid-1234",
		SpireJoinToken:        "spire-token-abc",
		ControlTunnelEndpoint: "api.internal:8443",
		ControlTunnelSpiffeID: "spiffe://penguintech.io/beta/api-manager",
	}

	tests := []struct {
		name       string
		status     int
		body       string
		token      string
		wantErr    bool
		wantStatus bool // true when the error must be a *StatusError
		wantPerm   bool
	}{
		{name: "created returns response", status: http.StatusCreated, body: "", token: "tok"},
		{name: "ok also accepted", status: http.StatusOK, body: "", token: "tok"},
		{name: "unauthorized is permanent", status: http.StatusUnauthorized, token: "bad", wantErr: true, wantStatus: true, wantPerm: true},
		{name: "forbidden is permanent", status: http.StatusForbidden, token: "bad", wantErr: true, wantStatus: true, wantPerm: true},
		{name: "server error is transient", status: http.StatusInternalServerError, token: "tok", wantErr: true, wantStatus: true},
		{name: "bad gateway is transient", status: http.StatusBadGateway, token: "tok", wantErr: true, wantStatus: true},
		{name: "malformed body", status: http.StatusCreated, body: "not json", token: "tok", wantErr: true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cert := testutil.NewSelfSignedCert(t)
			caPath := testutil.WriteCAFile(t, cert)

			srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodPost {
					t.Errorf("method = %q, want POST", r.Method)
				}
				if r.URL.Path != "/api/v1/nodes/discover" {
					t.Errorf("path = %q, want /api/v1/nodes/discover", r.URL.Path)
				}
				if got, want := r.Header.Get("Authorization"), "Bearer "+tc.token; got != want {
					t.Errorf("Authorization = %q, want %q", got, want)
				}
				if got := r.Header.Get("Content-Type"); got != "application/json" {
					t.Errorf("Content-Type = %q, want application/json", got)
				}
				var req discover.Request
				if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
					t.Errorf("decode request: %v", err)
				}
				if req.PrimaryNICMAC != "aa:bb:cc:dd:ee:ff" {
					t.Errorf("PrimaryNICMAC = %q", req.PrimaryNICMAC)
				}
				w.WriteHeader(tc.status)
				if tc.body != "" {
					_, _ = w.Write([]byte(tc.body))
					return
				}
				if tc.status == http.StatusCreated || tc.status == http.StatusOK {
					_ = json.NewEncoder(w).Encode(okResp)
				}
			}))
			srv.TLS = &tls.Config{Certificates: []tls.Certificate{cert.TLS}, MinVersion: tls.VersionTLS12}
			srv.StartTLS()
			defer srv.Close()

			req := discover.Request{DMIUUID: "uuid-1", PrimaryNICMAC: "aa:bb:cc:dd:ee:ff", HardwareTags: []string{"disk:nvme"}}
			got, err := discover.Post(context.Background(), srv.URL, tc.token, caPath, req)

			if (err != nil) != tc.wantErr {
				t.Fatalf("Post error = %v, wantErr %v", err, tc.wantErr)
			}
			var se *discover.StatusError
			if gotStatus := errors.As(err, &se); gotStatus != tc.wantStatus {
				t.Errorf("StatusError present = %v, want %v (err=%v)", gotStatus, tc.wantStatus, err)
			}
			if got := discover.IsPermanent(err); got != tc.wantPerm {
				t.Errorf("IsPermanent = %v, want %v", got, tc.wantPerm)
			}
			if err == nil && got != okResp {
				t.Errorf("response = %+v, want %+v", got, okResp)
			}
		})
	}
}

func TestPost_Errors(t *testing.T) {
	tests := []struct {
		name    string
		url     func(t *testing.T) string
		caPath  func(t *testing.T) string
		wantSub string
	}{
		{
			name:    "connection refused",
			url:     func(*testing.T) string { return "https://127.0.0.1:1" },
			caPath:  func(*testing.T) string { return "" },
			wantSub: "discover: post",
		},
		{
			name:    "invalid url control character",
			url:     func(*testing.T) string { return "https://exa\x7fmple.invalid" },
			caPath:  func(*testing.T) string { return "" },
			wantSub: "new request",
		},
		{
			name:    "missing CA file",
			url:     func(*testing.T) string { return "https://127.0.0.1:1" },
			caPath:  func(t *testing.T) string { return filepath.Join(t.TempDir(), "absent.pem") },
			wantSub: "tls config",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := discover.Post(context.Background(), tc.url(t), "tok", tc.caPath(t), discover.Request{})
			if err == nil {
				t.Fatal("expected error")
			}
			if !strings.Contains(err.Error(), tc.wantSub) {
				t.Errorf("error %q does not contain %q", err, tc.wantSub)
			}
		})
	}
}

func TestPost_CancelledContext(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := discover.Post(ctx, srv.URL, "tok", "", discover.Request{})
	if !errors.Is(err, context.Canceled) {
		t.Errorf("error = %v, want context.Canceled", err)
	}
}

func TestIsPermanent(t *testing.T) {
	tests := []struct {
		name string
		err  error
		want bool
	}{
		{name: "nil", err: nil, want: false},
		{name: "plain error", err: fmt.Errorf("boom"), want: false},
		{name: "401", err: &discover.StatusError{Code: 401}, want: true},
		{name: "403", err: &discover.StatusError{Code: 403}, want: true},
		{name: "429 retryable", err: &discover.StatusError{Code: 429}, want: false},
		{name: "503 retryable", err: &discover.StatusError{Code: 503}, want: false},
		{name: "wrapped 401", err: fmt.Errorf("outer: %w", &discover.StatusError{Code: 401}), want: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := discover.IsPermanent(tc.err); got != tc.want {
				t.Errorf("IsPermanent = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestStatusError_Error(t *testing.T) {
	e := &discover.StatusError{Code: 500, URL: "https://api/x"}
	want := "discover: unexpected status 500 from https://api/x"
	if e.Error() != want {
		t.Errorf("Error() = %q, want %q", e.Error(), want)
	}
}

func TestSmartPreflight(t *testing.T) {
	tests := []struct {
		name    string
		results []probe.SmartOutput
		wantErr bool
	}{
		{name: "nil slice", results: nil},
		{name: "all healthy", results: []probe.SmartOutput{smartWith("/dev/sda", "A1", true), smartWith("/dev/sdb", "B2", true)}},
		{name: "one failed", results: []probe.SmartOutput{smartWith("/dev/sda", "A1", true), smartWith("/dev/sdb", "B2", false)}, wantErr: true},
		{name: "first failed short-circuits", results: []probe.SmartOutput{smartWith("/dev/sdc", "C3", false), smartWith("/dev/sdd", "D4", false)}, wantErr: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := discover.SmartPreflight(tc.results)
			if (err != nil) != tc.wantErr {
				t.Fatalf("SmartPreflight error = %v, wantErr %v", err, tc.wantErr)
			}
			if tc.wantErr && !strings.Contains(err.Error(), "SMART preflight failed") {
				t.Errorf("error %q missing preflight marker", err)
			}
		})
	}
}

func TestRequest_JSON(t *testing.T) {
	tests := []struct {
		name       string
		req        discover.Request
		wantKeys   []string
		absentKeys []string
	}{
		{
			name:       "minimal request omits optional objects",
			req:        discover.Request{DMIUUID: "uuid-1", PrimaryNICMAC: "aa:bb:cc:dd:ee:ff"},
			wantKeys:   []string{"dmi_uuid", "primary_nic_mac", "firmware_type", "smart_attributes", "nics", "accelerators", "hardware_tags"},
			absentKeys: []string{"lshw_json", "lsblk_json", "numa_topology", "hostname", "bmc_tls_status", "bmc_cert_sha256"},
		},
		{
			name: "raw objects are forwarded under hub field names",
			req: discover.Request{
				DMIUUID:       "uuid-1",
				LshwJSON:      json.RawMessage(`{"class":"system"}`),
				LsblkJSON:     json.RawMessage(`{"blockdevices":[]}`),
				BMCTLSStatus:  "ok",
				BMCCertSHA256: strings.Repeat("a", 64),
			},
			wantKeys:   []string{"lshw_json", "lsblk_json", "bmc_tls_status", "bmc_cert_sha256"},
			absentKeys: []string{"lshw", "lsblk", "mac_address"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			data, err := json.Marshal(tc.req)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			var m map[string]json.RawMessage
			if err := json.Unmarshal(data, &m); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			for _, k := range tc.wantKeys {
				if _, ok := m[k]; !ok {
					t.Errorf("key %q missing from %s", k, data)
				}
			}
			for _, k := range tc.absentKeys {
				if _, ok := m[k]; ok {
					t.Errorf("key %q should be omitted from %s", k, data)
				}
			}
		})
	}
}

// TestResponseUnmarshal pins the hub's real response shape: a success envelope with a numeric node_id
// plus the legacy bare body with a string node_id.
func TestResponseUnmarshal(t *testing.T) {
	tests := []struct {
		name string
		body string
		want discover.Response
	}{
		{
			name: "hub envelope with numeric node_id and bmc",
			body: `{"status":"success","data":{"node_id":42,"state":"probed","spire_join_token":null,` +
				`"control_tunnel_endpoint":"grpcs://h:8443","control_tunnel_spiffe_id":"spiffe://x/api"},"meta":{}}`,
			want: discover.Response{
				NodeID: "42", ControlTunnelEndpoint: "grpcs://h:8443", ControlTunnelSpiffeID: "spiffe://x/api",
			},
		},
		{
			name: "envelope with null spire token",
			body: `{"status":"success","data":{"node_id":7,"spire_join_token":null,"control_tunnel_endpoint":"e",` +
				`"control_tunnel_spiffe_id":"s"}}`,
			want: discover.Response{NodeID: "7", ControlTunnelEndpoint: "e", ControlTunnelSpiffeID: "s"},
		},
		{
			name: "bare body with string node_id",
			body: `{"node_id":"node-uuid-1","spire_join_token":"t","control_tunnel_endpoint":"e","control_tunnel_spiffe_id":"s"}`,
			want: discover.Response{NodeID: "node-uuid-1", SpireJoinToken: "t", ControlTunnelEndpoint: "e", ControlTunnelSpiffeID: "s"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var got discover.Response
			if err := json.Unmarshal([]byte(tc.body), &got); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			if got != tc.want {
				t.Fatalf("got %+v, want %+v", got, tc.want)
			}
		})
	}
	var bad discover.Response
	if err := json.Unmarshal([]byte(`{"node_id":`), &bad); err == nil {
		t.Fatal("want error for malformed JSON")
	}
	if err := json.Unmarshal([]byte(`{"data":{"node_id":{"x":1`), &bad); err == nil {
		t.Fatal("want error for truncated envelope")
	}
}
