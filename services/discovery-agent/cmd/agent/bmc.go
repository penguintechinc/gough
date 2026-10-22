//go:build noxdp

package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/cmdline"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/discover"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
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
)

// bmcTrustFor combines environment trust settings with the optional per-boot
// gough_bmc_sha256 cmdline pin. The cmdline pin takes precedence.
func bmcTrustFor(params cmdline.Params) (probe.BMCTrust, error) {
	trust, err := probe.BMCTrustFromEnv()
	if err != nil {
		return probe.BMCTrust{}, err
	}
	if params.BMCSHA256 != "" {
		trust.CertSHA256 = params.BMCSHA256
	}
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
