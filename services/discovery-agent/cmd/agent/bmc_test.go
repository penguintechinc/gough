//go:build noxdp

package main

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/cmdline"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/discover"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/tags"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/testutil"
)

// bmcFixture is a loopback TLS server standing in for a BMC.
type bmcFixture struct {
	addr  string // host:port
	sha   string // lowercase hex SHA-256 of the leaf DER
	cert  testutil.Cert
	conns *atomic.Int32
}

// startBMC runs a TLS server that counts accepted connections.
func startBMC(t *testing.T) bmcFixture {
	t.Helper()
	cert := testutil.NewSelfSignedCert(t)
	conns := &atomic.Int32{}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	srv.TLS = &tls.Config{Certificates: []tls.Certificate{cert.TLS}, MinVersion: tls.VersionTLS12}
	srv.Config.ConnState = func(_ net.Conn, s http.ConnState) {
		if s == http.StateNew {
			conns.Add(1)
		}
	}
	srv.StartTLS()
	t.Cleanup(srv.Close)

	sum := sha256.Sum256(cert.DER)
	return bmcFixture{
		addr:  strings.TrimPrefix(srv.URL, "https://"),
		sha:   hex.EncodeToString(sum[:]),
		cert:  cert,
		conns: conns,
	}
}

// clearBMCEnv pins both trust env vars to empty so the host environment cannot leak in.
func clearBMCEnv(t *testing.T) {
	t.Helper()
	t.Setenv("GOUGH_BMC_CA_FILE", "")
	t.Setenv("GOUGH_BMC_CERT_SHA256", "")
}

// baseParams returns cmdline params with the required keys set and no BMC keys.
func baseParams() cmdline.Params {
	return cmdline.Params{Token: "tok", Primary: "https://hub.example", MAC: "aa:bb:cc:dd:ee:ff"}
}

// TestAttachBMCStatus drives every BMC outcome through attachBMCStatus.
func TestAttachBMCStatus(t *testing.T) {
	wrongPin := strings.Repeat("aa", 32)

	tests := []struct {
		name       string
		bmc        func(f bmcFixture) string
		pin        func(f bmcFixture) string // cmdline gough_bmc_sha256
		envCA      func(t *testing.T, f bmcFixture) string
		envPin     string
		cancelled  bool
		wantStatus string
		wantErr    bool
		wantConns  int32
		wantSHA    bool // true when the observed leaf SHA must be recorded
	}{
		{
			name:       "no BMC address: not configured, no connection",
			bmc:        func(bmcFixture) string { return "" },
			wantStatus: bmcStatusNotConfigured,
		},
		{
			name:       "cmdline pin matches: ok",
			bmc:        func(f bmcFixture) string { return f.addr },
			pin:        func(f bmcFixture) string { return f.sha },
			wantStatus: bmcStatusOK, wantConns: 1, wantSHA: true,
		},
		{
			name:       "cmdline pin mismatch: cert_mismatch with observed SHA",
			bmc:        func(f bmcFixture) string { return f.addr },
			pin:        func(bmcFixture) string { return wrongPin },
			wantStatus: bmcStatusMismatch, wantConns: 1, wantSHA: true,
		},
		{
			name: "env CA bundle verifies: ok",
			bmc:  func(f bmcFixture) string { return f.addr },
			envCA: func(t *testing.T, f bmcFixture) string {
				return testutil.WriteCAFile(t, f.cert)
			},
			wantStatus: bmcStatusOK, wantConns: 1, wantSHA: true,
		},
		{
			name:       "cmdline pin conflicting with env pin: pin_conflict, no connection",
			bmc:        func(f bmcFixture) string { return f.addr },
			pin:        func(f bmcFixture) string { return f.sha },
			envPin:     wrongPin,
			wantStatus: bmcStatusPinConflict, wantConns: 0,
		},
		{
			name:       "no trust configured: tls_unverified, no connection",
			bmc:        func(f bmcFixture) string { return f.addr },
			wantStatus: bmcStatusUnverified, wantConns: 0,
		},
		{
			name:    "malformed cmdline pin is a fatal config error",
			bmc:     func(f bmcFixture) string { return f.addr },
			pin:     func(bmcFixture) string { return "xyz" },
			wantErr: true, wantConns: 0,
		},
		{
			name: "missing env CA bundle is a fatal config error",
			bmc:  func(f bmcFixture) string { return f.addr },
			envCA: func(t *testing.T, _ bmcFixture) string {
				return filepath.Join(t.TempDir(), "absent.pem")
			},
			wantErr: true, wantConns: 0,
		},
		{
			name:       "unreachable BMC is reported, not fatal",
			bmc:        func(bmcFixture) string { return "127.0.0.1:1" },
			pin:        func(f bmcFixture) string { return f.sha },
			wantStatus: bmcStatusUnreachable, wantConns: 0,
		},
		{
			name:      "cancelled context surfaces the context error",
			bmc:       func(f bmcFixture) string { return f.addr },
			pin:       func(f bmcFixture) string { return f.sha },
			cancelled: true,
			wantErr:   true, wantConns: 0,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			clearBMCEnv(t)
			f := startBMC(t)
			if tc.envPin != "" {
				t.Setenv("GOUGH_BMC_CERT_SHA256", tc.envPin)
			}
			if tc.envCA != nil {
				t.Setenv("GOUGH_BMC_CA_FILE", tc.envCA(t, f))
			}

			params := baseParams()
			params.BMC = tc.bmc(f)
			if tc.pin != nil {
				params.BMCSHA256 = tc.pin(f)
			}

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if tc.cancelled {
				cancel()
			}

			var req discover.Request
			err := attachBMCStatus(ctx, &req, params)

			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, wantErr %v", err, tc.wantErr)
			}
			if tc.cancelled && !errors.Is(err, context.Canceled) {
				t.Errorf("error = %v, want context.Canceled", err)
			}
			if got := f.conns.Load(); got != tc.wantConns {
				t.Errorf("BMC connections = %d, want %d", got, tc.wantConns)
			}
			if tc.wantErr {
				return
			}
			if req.BMCTLSStatus != tc.wantStatus {
				t.Errorf("BMCTLSStatus = %q, want %q", req.BMCTLSStatus, tc.wantStatus)
			}
			if tc.wantSHA && req.BMCCertSHA256 != f.sha {
				t.Errorf("BMCCertSHA256 = %q, want observed %q", req.BMCCertSHA256, f.sha)
			}
			if !tc.wantSHA && req.BMCCertSHA256 != "" {
				t.Errorf("BMCCertSHA256 = %q, want empty", req.BMCCertSHA256)
			}
		})
	}
}

// TestBuildPayloadPostsBMCStatus runs the payload path end to end: buildPayload
// probes the BMC, and discover.Post sends the result to a TLS hub, which must
// receive bmc_tls_status and bmc_cert_sha256 in the JSON body.
func TestBuildPayloadPostsBMCStatus(t *testing.T) {
	clearBMCEnv(t)
	bmc := startBMC(t)

	hubCert := testutil.NewSelfSignedCert(t)
	hubCA := testutil.WriteCAFile(t, hubCert)

	var mu sync.Mutex
	var captured map[string]json.RawMessage
	hub := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var m map[string]json.RawMessage
		if err := json.NewDecoder(r.Body).Decode(&m); err == nil {
			mu.Lock()
			captured = m
			mu.Unlock()
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"node_id":"n-1","state":"probed"}`))
	}))
	hub.TLS = &tls.Config{Certificates: []tls.Certificate{hubCert.TLS}, MinVersion: tls.VersionTLS12}
	hub.StartTLS()
	t.Cleanup(hub.Close)

	params := baseParams()
	params.Primary = hub.URL
	params.BMC = bmc.addr
	params.BMCSHA256 = bmc.sha

	hw := tags.HardwareInput{DMI: probe.DMIInfo{ProductUUID: "uuid-bmc"}, Firmware: probe.FirmwareUEFI}
	req, err := buildPayload(context.Background(), params, hw, []string{"cpu:vendor:intel"})
	if err != nil {
		t.Fatalf("buildPayload: %v", err)
	}
	if _, err := discover.Post(context.Background(), params.Primary, params.Token, hubCA, req); err != nil {
		t.Fatalf("discover.Post: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	var status, sha string
	if err := json.Unmarshal(captured["bmc_tls_status"], &status); err != nil {
		t.Fatalf("bmc_tls_status missing from posted body: %v", err)
	}
	if err := json.Unmarshal(captured["bmc_cert_sha256"], &sha); err != nil {
		t.Fatalf("bmc_cert_sha256 missing from posted body: %v", err)
	}
	if status != bmcStatusOK {
		t.Errorf("posted bmc_tls_status = %q, want %q", status, bmcStatusOK)
	}
	if sha != bmc.sha {
		t.Errorf("posted bmc_cert_sha256 = %q, want %q", sha, bmc.sha)
	}
}

// TestBuildPayloadFatalOnBadTrust asserts a misconfigured pin stops payload construction.
func TestBuildPayloadFatalOnBadTrust(t *testing.T) {
	clearBMCEnv(t)
	params := baseParams()
	params.BMC = "127.0.0.1:1"
	params.BMCSHA256 = "not-a-pin"

	if _, err := buildPayload(context.Background(), params, tags.HardwareInput{}, nil); err == nil {
		t.Fatal("buildPayload with malformed pin: expected error")
	}
}

// TestValidateHubBMC covers validating the BMC the hub reports in its authenticated response.
func TestValidateHubBMC(t *testing.T) {
	bmc := startBMC(t)

	t.Run("no hub bmc is a no-op", func(t *testing.T) {
		clearBMCEnv(t)
		status, err := validateHubBMC(context.Background(), baseParams(), discover.Response{})
		if err != nil || status != "" {
			t.Fatalf("got (%q, %v), want (\"\", nil)", status, err)
		}
		if bmc.conns.Load() != 0 {
			t.Fatalf("BMC contacted with nothing to validate")
		}
	})

	t.Run("cmdline-configured bmc is not re-probed", func(t *testing.T) {
		clearBMCEnv(t)
		p := baseParams()
		p.BMC = "192.0.2.1"
		status, err := validateHubBMC(context.Background(), p, discover.Response{BMCAddress: bmc.addr})
		if err != nil || status != "" {
			t.Fatalf("got (%q, %v), want (\"\", nil)", status, err)
		}
	})

	t.Run("matching hub pin validates", func(t *testing.T) {
		clearBMCEnv(t)
		status, err := validateHubBMC(context.Background(), baseParams(),
			discover.Response{BMCAddress: bmc.addr, BMCCertSHA256: bmc.sha})
		if err != nil || status != bmcStatusOK {
			t.Fatalf("got (%q, %v), want (%q, nil)", status, err, bmcStatusOK)
		}
	})

	t.Run("wrong hub pin is a mismatch", func(t *testing.T) {
		clearBMCEnv(t)
		status, err := validateHubBMC(context.Background(), baseParams(),
			discover.Response{BMCAddress: bmc.addr, BMCCertSHA256: strings.Repeat("00", 32)})
		if err != nil || status != bmcStatusMismatch {
			t.Fatalf("got (%q, %v), want (%q, nil)", status, err, bmcStatusMismatch)
		}
	})

	t.Run("env pin outranks conflicting hub pin", func(t *testing.T) {
		clearBMCEnv(t)
		t.Setenv("GOUGH_BMC_CERT_SHA256", bmc.sha)
		status, err := validateHubBMC(context.Background(), baseParams(),
			discover.Response{BMCAddress: bmc.addr, BMCCertSHA256: strings.Repeat("11", 32)})
		if err != nil || status != bmcStatusPinConflict {
			t.Fatalf("got (%q, %v), want (%q, nil)", status, err, bmcStatusPinConflict)
		}
	})

	t.Run("invalid env trust is an error", func(t *testing.T) {
		clearBMCEnv(t)
		t.Setenv("GOUGH_BMC_CERT_SHA256", "not-hex")
		if _, err := validateHubBMC(context.Background(), baseParams(),
			discover.Response{BMCAddress: bmc.addr}); err == nil {
			t.Fatal("want error for malformed configured pin")
		}
	})
}
