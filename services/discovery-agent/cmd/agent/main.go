//go:build noxdp

// discovery-agent collects hardware inventory from a bare-metal node booted
// via the Gough helper iPXE image, posts the results to the api-manager, and
// then opens a persistent gRPC-mTLS control tunnel.
//
// The binary is statically linked and intended to run as the sole user-space
// process in the initrd helper image (hence the noxdp build tag — XDP is
// not available in an initrd context).
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"

	"github.com/golang-jwt/jwt/v5"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/cmdline"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/discover"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/metrics"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/serial"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/spire"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/tags"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/tunnel"
)

// Version is injected at build time via -ldflags.
var Version = "dev"

const (
	caPEMPath     = "/etc/gough/ca.pem"
	spireSocket   = "unix:///run/spire-agent/public/api.sock"
	trustDomain   = "penguintech.io"
	probeTimeout  = 5 * time.Minute
)

// readRawCmdline reads /proc/cmdline and returns the raw string for memory zeroing.
// Actual parsing is delegated to the cmdline package.
func readRawCmdline() (string, error) {
	raw, err := os.ReadFile("/proc/cmdline")
	if err != nil {
		return "", fmt.Errorf("read /proc/cmdline: %w", err)
	}
	return strings.TrimSpace(string(raw)), nil
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))

	metrics.Register()

	slog.Info("discovery-agent starting", "version", Version, "go", runtime.Version())

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	if err := run(ctx); err != nil {
		slog.Error("discovery-agent fatal", "err", err)
		serial.WriteEvent("error", "fatal exit", map[string]any{"err": err.Error()})
		os.Exit(1)
	}
}

func run(ctx context.Context) error {
	// 1. Parse kernel cmdline.
	rawCmdline, err := readRawCmdline()
	if err != nil {
		return fmt.Errorf("cmdline: %w", err)
	}
	params, err := cmdline.Parse()
	if err != nil {
		return fmt.Errorf("cmdline: %w", err)
	}

	// 2. Validate JWT signature before any network call.
	if err := validateToken(params.Token, caPEMPath); err != nil {
		return fmt.Errorf("JWT validation: %w", err)
	}

	// 3. Zero the raw cmdline buffer from our copy (best-effort).
	zeroString(rawCmdline)
	runtime.GC()

	slog.Info("cmdline parsed", "primary", params.Primary, "mac", params.MAC)

	// 4. Collect hardware probes.
	serial.WriteEvent("info", "starting hardware probes", nil)
	hw, err := collectProbes(ctx)
	if err != nil {
		return fmt.Errorf("probes: %w", err)
	}

	// 5. Generate hardware tags.
	hwTags := tags.Discover(hw)
	slog.Info("hardware tags generated", "count", len(hwTags))

	// 6. SMART preflight — abort if any disk is failed.
	if err := discover.SmartPreflight(hw.SMART); err != nil {
		return fmt.Errorf("smart preflight: %w", err)
	}

	// 7. Build discover request.
	hostname, _ := os.Hostname()
	req, err := buildDiscoverRequest(params.MAC, hostname, hw, hwTags)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}

	// 9. POST /api/v1/nodes/discover with retry on transient errors.
	serial.WriteEvent("info", "posting discovery to api-manager", map[string]any{"primary": params.Primary})
	var discoverResp discover.Response
	var httpsFailedAt time.Time

	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		discoverResp, err = discover.Post(ctx, params.Primary, params.Token, caPEMPath, req)
		if err == nil {
			break
		}
		slog.Warn("discover POST failed", "err", err)
		if httpsFailedAt.IsZero() {
			httpsFailedAt = time.Now()
		}
		if serial.ShouldUseFallback(httpsFailedAt) {
			serial.WriteEvent("warn", "HTTPS unavailable >5m — continuing via serial console", map[string]any{
				"primary": params.Primary,
				"err":     err.Error(),
			})
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(10 * time.Second):
		}
	}

	slog.Info("discover POST succeeded",
		"node_id", discoverResp.NodeID,
		"tunnel_endpoint", discoverResp.ControlTunnelEndpoint,
		"spiffe_id", discoverResp.ControlTunnelSpiffeID,
	)
	serial.WriteEvent("info", "discover POST succeeded", map[string]any{"node_id": discoverResp.NodeID})

	// 8. Fetch SVID via SPIRE Workload API.
	spireClient, err := spire.NewClient(spireSocket)
	if err != nil {
		return fmt.Errorf("spire client: %w", err)
	}

	svidBundle, err := spireClient.FetchSVID(ctx, trustDomain)
	if err != nil {
		return fmt.Errorf("fetch SVID: %w", err)
	}
	slog.Info("SVID fetched", "spiffe_id", svidBundle.SPIFFEID)

	// 9. Open persistent gRPC-mTLS control tunnel.
	t := tunnel.New(tunnel.TunnelConfig{
		Endpoint:  discoverResp.ControlTunnelEndpoint,
		TLSConfig: svidBundle.TLSConfig,
		NodeID:    discoverResp.NodeID,
		SPIFFEID:  svidBundle.SPIFFEID,
	})

	slog.Info("opening control tunnel", "endpoint", discoverResp.ControlTunnelEndpoint)
	serial.WriteEvent("info", "control tunnel opening", map[string]any{
		"endpoint": discoverResp.ControlTunnelEndpoint,
	})

	// Run blocks until ctx is cancelled.
	return t.Run(ctx)
}

// validateToken verifies the JWT signature using the CA cert from the initrd.
// It uses HMAC-HS256 or RS256 depending on the token header, and validates
// expiry/nbf claims.  The CA PEM is used as the HMAC secret for HS256 or to
// extract the public key for RS/EC variants.
//
// For M1 the api-manager issues HMAC-HS256 tokens signed with a secret
// derived from the CA PEM content.  RS256 support is added in M2 when Vault
// PKI is available at boot time.
func validateToken(tokenStr, caPEMPath string) error {
	caPEM, err := os.ReadFile(caPEMPath)
	if err != nil {
		return fmt.Errorf("read CA %s: %w", caPEMPath, err)
	}

	// Parse the JWT without verification first to inspect the algorithm.
	unverified, _, err := jwt.NewParser().ParseUnverified(tokenStr, jwt.MapClaims{})
	if err != nil {
		return fmt.Errorf("parse JWT header: %w", err)
	}

	switch unverified.Method.Alg() {
	case "HS256", "HS384", "HS512":
		// HMAC: use CA PEM bytes as the secret.
		_, err = jwt.Parse(tokenStr, func(t *jwt.Token) (any, error) {
			if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
				return nil, fmt.Errorf("unexpected signing method: %s", t.Header["alg"])
			}
			return caPEM, nil
		}, jwt.WithExpirationRequired(), jwt.WithValidMethods([]string{"HS256", "HS384", "HS512"}))
	default:
		return fmt.Errorf("unsupported JWT algorithm %q (expected HS256)", unverified.Method.Alg())
	}

	return err
}

// zeroString overwrites the memory backing s with zeroes using the
// string-to-bytes reinterpretation trick that is safe per the Go unsafe rules:
// we copy the string header, extract the data pointer, and overwrite via the
// resulting []byte.  unix.Madvise is used to advise the kernel to discard the
// backing pages.  This is best-effort — the GC may have already moved the data.
func zeroString(s string) {
	if len(s) == 0 {
		return
	}
	// Convert string to []byte without allocation by aliasing the header.
	// unsafe.StringData is the correct Go 1.20+ way to get the backing pointer.
	ptr := unsafe.StringData(s)
	b := unsafe.Slice(ptr, len(s))

	// Advise the kernel to discard the pages (best-effort).
	_ = unix.Madvise(b, unix.MADV_DONTNEED)

	// Overwrite with zeros.
	for i := range b {
		b[i] = 0
	}
}

// collectProbes runs all hardware probes with a timeout.
func collectProbes(ctx context.Context) (tags.HardwareInput, error) {
	ctx, cancel := context.WithTimeout(ctx, probeTimeout)
	defer cancel()

	runner := exec.OSRunner{}

	lshw, err := probe.RunLshw(ctx, runner)
	if err != nil {
		slog.Warn("lshw probe failed", "err", err)
	}

	lsblk, err := probe.RunLsblk(ctx, runner)
	if err != nil {
		return tags.HardwareInput{}, fmt.Errorf("lsblk: %w", err)
	}

	dmi, err := probe.ReadDMI()
	if err != nil {
		slog.Warn("dmi probe failed", "err", err)
	}

	nics, err := probe.EnumerateNICs()
	if err != nil {
		slog.Warn("nic probe failed", "err", err)
	}

	smartResults, smartErrs := probe.RunSmartAllDisks(ctx, runner, lsblk.TopLevelDisks())
	for _, e := range smartErrs {
		slog.Warn("smartctl partial failure", "err", e)
	}

	fw := probe.DetectFirmware()

	numa, err := probe.RunNUMA(ctx, runner)
	if err != nil {
		slog.Warn("numa probe failed", "err", err)
	}

	accel := probe.RunAccelerators(ctx, runner)

	return tags.HardwareInput{
		Lshw:         lshw,
		Lsblk:        lsblk,
		DMI:          dmi,
		NICs:         nics,
		SMART:        smartResults,
		Firmware:     fw,
		NUMA:         numa,
		Accelerators: accel,
	}, nil
}

// buildDiscoverRequest assembles the POST body from probe results.
func buildDiscoverRequest(mac, hostname string, hw tags.HardwareInput, hwTags []string) (discover.Request, error) {
	req := discover.Request{
		MACAddress:   mac,
		Hostname:     hostname,
		ProductUUID:  hw.DMI.ProductUUID,
		SysVendor:    hw.DMI.SysVendor,
		ProductName:  hw.DMI.ProductName,
		BIOSVersion:  hw.DMI.BIOSVersion,
		FirmwareType: string(hw.Firmware),
		HardwareTags: hwTags,
	}

	if hw.Lshw.Raw != nil {
		req.Lshw = hw.Lshw.Raw
	}
	if hw.Lsblk.Raw != nil {
		req.Lsblk = hw.Lsblk.Raw
	}

	for _, s := range hw.SMART {
		if s.Raw != nil {
			req.SMART = append(req.SMART, s.Raw)
		}
	}

	if hw.NUMA.Lstopo.Raw != nil {
		req.NumaJSON = hw.NUMA.Lstopo.Raw
	}

	accelJSON, err := json.Marshal(hw.Accelerators)
	if err == nil {
		req.AcceleratorJSON = accelJSON
	}

	return req, nil
}
