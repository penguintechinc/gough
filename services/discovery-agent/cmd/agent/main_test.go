//go:build noxdp

package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/tags"
)

// writeSecret writes the HMAC secret used as the CA PEM stand-in and returns its path.
func writeSecret(t *testing.T, secret string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(p, []byte(secret), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

// signHS256 returns a compact HS256 token with the given claims signed by key.
func signHS256(t *testing.T, key string, claims jwt.MapClaims) string {
	t.Helper()
	tok, err := jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString([]byte(key))
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	return tok
}

// TestValidateToken covers signature, expiry, algorithm, and CA-file handling.
func TestValidateToken(t *testing.T) {
	const secret = "ca-pem-secret"
	future := jwt.MapClaims{"exp": time.Now().Add(time.Hour).Unix()}
	past := jwt.MapClaims{"exp": time.Now().Add(-time.Hour).Unix()}

	noneTok, err := jwt.NewWithClaims(jwt.SigningMethodNone, future).SignedString(jwt.UnsafeAllowNoneSignatureType)
	if err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name    string
		token   func() string
		caPath  func(t *testing.T) string
		wantErr string
	}{
		{name: "valid HS256", token: func() string { return signHS256(t, secret, future) }, caPath: func(t *testing.T) string { return writeSecret(t, secret) }},
		{name: "wrong secret", token: func() string { return signHS256(t, "other", future) }, caPath: func(t *testing.T) string { return writeSecret(t, secret) }, wantErr: "signature"},
		{name: "expired", token: func() string { return signHS256(t, secret, past) }, caPath: func(t *testing.T) string { return writeSecret(t, secret) }, wantErr: "expired"},
		{name: "no exp claim", token: func() string { return signHS256(t, secret, jwt.MapClaims{}) }, caPath: func(t *testing.T) string { return writeSecret(t, secret) }, wantErr: "exp"},
		{name: "alg none rejected", token: func() string { return noneTok }, caPath: func(t *testing.T) string { return writeSecret(t, secret) }, wantErr: "unsupported JWT algorithm"},
		{name: "malformed token", token: func() string { return "not.a.jwt" }, caPath: func(t *testing.T) string { return writeSecret(t, secret) }, wantErr: "parse JWT header"},
		{name: "missing CA file", token: func() string { return signHS256(t, secret, future) }, caPath: func(t *testing.T) string { return filepath.Join(t.TempDir(), "none.pem") }, wantErr: "read CA"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := validateToken(tc.token(), tc.caPath(t))
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("validateToken: %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("error = %v, want substring %q", err, tc.wantErr)
			}
		})
	}
}

// TestReadRawCmdline reads the live kernel cmdline when present (Linux hosts).
func TestReadRawCmdline(t *testing.T) {
	if _, err := os.Stat("/proc/cmdline"); err != nil {
		t.Skipf("no /proc/cmdline: %v", err)
	}
	raw, err := readRawCmdline()
	if err != nil {
		t.Fatalf("readRawCmdline: %v", err)
	}
	if strings.HasSuffix(raw, "\n") {
		t.Errorf("result not trimmed: %q", raw)
	}
	if raw == "" {
		t.Error("expected non-empty cmdline on a running kernel")
	}
}

// TestZeroString verifies the best-effort wipe overwrites runtime-allocated bytes and tolerates empty input.
func TestZeroString(t *testing.T) {
	tests := []struct {
		name string
		in   func() string
	}{
		{name: "empty is a no-op", in: func() string { return "" }},
		{name: "runtime string is zeroed", in: func() string { return strings.Repeat("s3cr3t-token-", 8) }},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			s := tc.in()
			zeroString(s)
			for i := 0; i < len(s); i++ {
				if s[i] != 0 {
					t.Fatalf("byte %d = %#x after zeroString, want 0", i, s[i])
				}
			}
		})
	}
}

// TestBuildDiscoverRequest checks field mapping and raw-blob forwarding rules.
func TestBuildDiscoverRequest(t *testing.T) {
	smartOK := probe.SmartOutput{}
	smartOK.Device.Name = "sda"

	tests := []struct {
		name         string
		hw           tags.HardwareInput
		wantLshw     bool
		wantLsblk    bool
		wantSMART    int
		wantNuma     bool
		wantFirmware probe.FirmwareType
	}{
		{name: "empty hardware", hw: tags.HardwareInput{}, wantFirmware: ""},
		{
			name: "raw blobs forwarded, smart without raw skipped",
			hw: tags.HardwareInput{
				Lshw:     probe.LshwOutput{Raw: json.RawMessage(`{"class":"system"}`)},
				Lsblk:    probe.LsblkOutput{Raw: json.RawMessage(`{"blockdevices":[]}`)},
				SMART:    []probe.SmartOutput{{Raw: json.RawMessage(`{"a":1}`)}, smartOK},
				NUMA:     probe.NUMAProbeResult{Lstopo: probe.LstopoOutput{Raw: json.RawMessage(`{"topo":true}`)}},
				Firmware: probe.FirmwareUEFI,
				DMI:      probe.DMIInfo{ProductUUID: "uuid-1", SysVendor: "Dell", ProductName: "R640", BIOSVersion: "2.1"},
			},
			wantLshw: true, wantLsblk: true, wantSMART: 1, wantNuma: true, wantFirmware: probe.FirmwareUEFI,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			hwTags := []string{"cpu:vendor:intel"}
			req, err := buildDiscoverRequest("aa:bb:cc:dd:ee:ff", "node-01", tc.hw, hwTags)
			if err != nil {
				t.Fatalf("buildDiscoverRequest: %v", err)
			}
			if req.MACAddress != "aa:bb:cc:dd:ee:ff" || req.Hostname != "node-01" {
				t.Errorf("mac/hostname = %q/%q", req.MACAddress, req.Hostname)
			}
			if req.ProductUUID != tc.hw.DMI.ProductUUID || req.SysVendor != tc.hw.DMI.SysVendor {
				t.Errorf("DMI fields not mapped: %+v", req)
			}
			if string(req.FirmwareType) != string(tc.wantFirmware) {
				t.Errorf("FirmwareType = %q, want %q", req.FirmwareType, tc.wantFirmware)
			}
			if len(req.HardwareTags) != 1 {
				t.Errorf("HardwareTags = %v", req.HardwareTags)
			}
			if (req.Lshw != nil) != tc.wantLshw {
				t.Errorf("Lshw present = %v, want %v", req.Lshw != nil, tc.wantLshw)
			}
			if (req.Lsblk != nil) != tc.wantLsblk {
				t.Errorf("Lsblk present = %v, want %v", req.Lsblk != nil, tc.wantLsblk)
			}
			if len(req.SMART) != tc.wantSMART {
				t.Errorf("SMART entries = %d, want %d", len(req.SMART), tc.wantSMART)
			}
			if (req.NumaJSON != nil) != tc.wantNuma {
				t.Errorf("NumaJSON present = %v, want %v", req.NumaJSON != nil, tc.wantNuma)
			}
			if len(req.AcceleratorJSON) == 0 {
				t.Error("AcceleratorJSON should always be marshalled")
			}
		})
	}
}

// TestCollectProbesWith drives the probe pipeline with canned tool output.
func TestCollectProbesWith(t *testing.T) {
	lsblkOK := []byte(`{"blockdevices":[{"name":"sda","type":"disk"}]}`)

	tests := []struct {
		name      string
		runner    exec.Runner
		wantErr   string
		wantLsblk bool
	}{
		{
			name:    "lsblk failure is fatal",
			runner:  &exec.MockRunner{Responses: map[string]exec.MockResponse{}},
			wantErr: "lsblk",
		},
		{
			name: "lsblk success tolerates other probe failures",
			runner: &exec.MockRunner{Responses: map[string]exec.MockResponse{
				"lsblk": {Output: lsblkOK},
			}},
			wantLsblk: true,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			hw, err := collectProbesWith(context.Background(), tc.runner)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("error = %v, want substring %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("collectProbesWith: %v", err)
			}
			if (hw.Lsblk.Raw != nil) != tc.wantLsblk {
				t.Errorf("Lsblk raw present = %v, want %v", hw.Lsblk.Raw != nil, tc.wantLsblk)
			}
			if hw.Lshw.Raw != nil {
				t.Error("lshw not mocked; Raw should stay nil")
			}
			if len(hw.SMART) != 0 {
				t.Errorf("smartctl not mocked; SMART = %d, want 0", len(hw.SMART))
			}
		})
	}
}

// TestCollectProbes runs the production wrapper against the host; only the
// absence of a hard error on a host with lsblk is asserted.
func TestCollectProbes(t *testing.T) {
	if _, err := os.Stat("/usr/bin/lsblk"); err != nil {
		t.Skip("lsblk not installed on this host")
	}
	if _, err := collectProbes(context.Background()); err != nil {
		t.Fatalf("collectProbes on host with lsblk: %v", err)
	}
}

// TestRunFailsBeforeNetworkWithoutCmdline asserts run() stops at cmdline parsing when the
// kernel command line lacks gough_ parameters, before any network call is made.
func TestRunFailsBeforeNetworkWithoutCmdline(t *testing.T) {
	raw, err := readRawCmdline()
	if err != nil {
		t.Skipf("no cmdline: %v", err)
	}
	if strings.Contains(raw, "gough_token=") {
		t.Skip("host cmdline carries gough_ parameters; run() would proceed")
	}
	err = run(context.Background())
	if err == nil || !strings.HasPrefix(err.Error(), "cmdline:") {
		t.Fatalf("run error = %v, want cmdline: prefix", err)
	}
	if errors.Is(err, context.Canceled) {
		t.Errorf("unexpected cancellation: %v", err)
	}
}
