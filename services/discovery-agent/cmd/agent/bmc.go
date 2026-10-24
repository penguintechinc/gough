//go:build noxdp

package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/cmdline"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/discover"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/serial"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/tags"
)

// Discovery-payload BMC status values. The probe's verified_* outcomes are
// reported to the hub as "ok" so operators see one healthy value.
const (
	bmcStatusOK            = "ok"
	bmcStatusUnverified    = "bmc_tls_unverified"
	bmcStatusMismatch      = "bmc_cert_mismatch"
	bmcStatusUnreachable   = "bmc_unreachable"
	bmcStatusNotConfigured = "bmc_not_configured"
	bmcStatusPinConflict   = "bmc_pin_conflict"
)

// errPinConflict reports a cmdline gough_bmc_sha256 that differs from the
// operator-configured GOUGH_BMC_CERT_SHA256. The cmdline is unauthenticated, so
// the configured pin always wins and no connection is made.
var errPinConflict = errors.New("bmc: cmdline gough_bmc_sha256 conflicts with configured GOUGH_BMC_CERT_SHA256")

// bmcTrustFor combines environment trust settings with the optional per-boot
// gough_bmc_sha256 cmdline pin. The cmdline is unauthenticated, so a configured
// env pin is authoritative: a differing cmdline pin returns errPinConflict. A
// cmdline pin is used only when no env pin is set, which is the weaker path.
func bmcTrustFor(params cmdline.Params) (probe.BMCTrust, error) {
	trust, err := probe.BMCTrustFromEnv()
	if err != nil {
		return probe.BMCTrust{}, err
	}
	if params.BMCSHA256 == "" {
		return trust, nil
	}
	cmdPin := strings.ToLower(strings.TrimSpace(params.BMCSHA256))
	if trust.CertSHA256 != "" {
		if cmdPin != trust.CertSHA256 {
			return probe.BMCTrust{}, errPinConflict
		}
		return trust, nil
	}
	trust.CertSHA256 = cmdPin
	return trust, nil
}

// attachBMCStatus probes the BMC named by params.BMC and records the outcome on
// req. Every BMC connection goes through probe.ValidateBMCCert. A misconfigured
// trust source is a returned error so the agent stops. An unreachable BMC is
// recorded as bmc_unreachable, because the BMC is optional for discovery.
func attachBMCStatus(ctx context.Context, req *discover.Request, params cmdline.Params) error {
	if params.BMC == "" {
		req.BMCTLSStatus = bmcStatusNotConfigured
		return nil
	}

	trust, err := bmcTrustFor(params)
	if errors.Is(err, errPinConflict) {
		req.BMCTLSStatus = bmcStatusPinConflict
		slog.Warn("bmc pin conflict: cmdline pin differs from configured pin; BMC not contacted", "bmc", params.BMC)
		return nil
	}
	if err != nil {
		return fmt.Errorf("bmc trust: %w", err)
	}
	if err := trust.Validate(); err != nil {
		return fmt.Errorf("bmc trust: %w", err)
	}

	res, err := probe.ValidateBMCCert(ctx, params.BMC, trust)
	if err != nil {
		if ctxErr := ctx.Err(); ctxErr != nil {
			return ctxErr
		}
		slog.Warn("bmc probe failed", "err", err)
		req.BMCTLSStatus = bmcStatusUnreachable
		return nil
	}

	req.BMCCertSHA256 = res.Thumbprint
	switch res.Status {
	case probe.BMCStatusVerifiedCA, probe.BMCStatusVerifiedPin:
		req.BMCTLSStatus = bmcStatusOK
	case probe.BMCStatusCertMismatch:
		req.BMCTLSStatus = bmcStatusMismatch
		slog.Warn("bmc certificate does not match pin", "bmc", params.BMC, "observed_sha256", res.Thumbprint)
	case probe.BMCStatusUnverified:
		req.BMCTLSStatus = bmcStatusUnverified
		slog.Warn("bmc certificate not verified: no CA or pin configured", "bmc", params.BMC)
	default:
		return fmt.Errorf("bmc: unexpected probe status %q", res.Status)
	}
	return nil
}

// buildPayload assembles the full discovery POST body for run(): host identity
// and hardware first, then the BMC TLS status.
func buildPayload(ctx context.Context, params cmdline.Params, hw tags.HardwareInput, hwTags []string) (discover.Request, error) {
	hostname, _ := os.Hostname()
	req, err := buildDiscoverRequest(params.MAC, hostname, hw, hwTags)
	if err != nil {
		return discover.Request{}, fmt.Errorf("build request: %w", err)
	}
	if err := attachBMCStatus(ctx, &req, params); err != nil {
		return discover.Request{}, err
	}
	return req, nil
}

// validateHubBMC runs the BMC TLS validation for a BMC the hub reported in its
// authenticated discover response, when none was configured at boot. The hub's
// address/pin never travel on the anonymous iPXE cmdline. The discover POST is
// single-shot (the bootstrap token is one-time), so the outcome is logged and
// emitted on the serial console; reporting it back to the hub needs a follow-up
// endpoint. It returns "" when there is nothing to validate.
func validateHubBMC(ctx context.Context, params cmdline.Params, resp discover.Response) (string, error) {
	if params.BMC != "" || resp.BMCAddress == "" {
		return "", nil
	}
	params.BMC = resp.BMCAddress
	params.BMCSHA256 = resp.BMCCertSHA256
	var scratch discover.Request
	if err := attachBMCStatus(ctx, &scratch, params); err != nil {
		return "", err
	}
	slog.Info("hub-provided bmc validated", "bmc", resp.BMCAddress, "status", scratch.BMCTLSStatus)
	serial.WriteEvent("info", "hub-provided bmc validated", map[string]any{
		"bmc":    resp.BMCAddress,
		"status": scratch.BMCTLSStatus,
	})
	return scratch.BMCTLSStatus, nil
}
