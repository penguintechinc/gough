//go:build noxdp

package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newBiomeCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "biome",
		Short: "Manage Gough biomes (workload catalog)",
		Long: `Manage biomes — the atomic deployment units in the Gough catalog.

Biomes declare workload_type (lxc|vm|k8s-helm|k8s-manifest), phase,
biome_kind, readiness probes, resource requirements, and upgrade strategy.`,
	}

	cmd.AddCommand(
		biomeListCmd(),
		biomeShowCmd(),
		biomeNewCmd(),
		biomeValidateCmd(),
		biomePublishCmd(),
		biomePromoteCmd(),
		biomeRollbackCmd(),
		biomeDiffCmd(),
		biomeReSignCmd(),
		biomeUpgradeCmd(),
		biomeEligibilityCheckCmd(),
		biomeDeployCmd(),
	)
	return cmd
}

func biomeListCmd() *cobra.Command {
	var (
		kind     string
		phase    string
		workload string
	)

	cmd := &cobra.Command{
		Use:   "list",
		Short: "List available biomes",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			biomes, err := c.ListBiomes(ctx, client.ListBiomesParams{
				Kind:     kind,
				Phase:    phase,
				Workload: workload,
			})
			if err != nil {
				return handleAPIError(err, w)
			}

			if len(biomes) == 0 {
				w.Infof("No biomes found")
				return nil
			}

			rows := make([][]string, 0, len(biomes))
			for _, e := range biomes {
				locked := "no"
				if e.LockToHost {
					locked = "yes"
				}
				rows = append(rows, []string{
					e.Name, e.Version, e.Kind, e.Phase,
					e.WorkloadType, locked,
				})
			}
			w.PrintTable(
				[]string{"NAME", "VERSION", "KIND", "PHASE", "WORKLOAD", "LOCKED"},
				rows,
			)
			return nil
		},
	}

	cmd.Flags().StringVar(&kind, "kind", "", "Filter by biome_kind")
	cmd.Flags().StringVar(&phase, "phase", "", "Filter by phase")
	cmd.Flags().StringVar(&workload, "workload", "", "Filter by workload_type")
	return cmd
}

func biomeShowCmd() *cobra.Command {
	var version string

	cmd := &cobra.Command{
		Use:   "show <name>",
		Short: "Show biome details",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			biome, err := c.ShowBiome(ctx, args[0], version)
			if err != nil {
				return handleAPIError(err, w)
			}
			w.PrintObject(biome)
			return nil
		},
	}

	cmd.Flags().StringVar(&version, "version", "", "Show a specific version")
	return cmd
}

func biomeNewCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "new <name>",
		Short: "Scaffold a new biome directory",
		Long: `Scaffold a new biome directory at the current working directory.

Creates:
  <name>/biome.yaml           — metadata manifest
  <name>/cloud-init/          — user-data and network-config templates
  <name>/lxd-profile.yaml    — LXD profile definition
  <name>/image-ref.txt        — OCI image reference
  <name>/hooks/               — post-deploy, pre-upgrade, post-upgrade hooks
  <name>/tests/smoke.sh       — smoke test script`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]
			return scaffoldBiome(name)
		},
	}
}

// scaffoldBiome creates the biome directory skeleton on the local filesystem.
func scaffoldBiome(name string) error {
	dirs := []string{
		name,
		name + "/cloud-init",
		name + "/hooks",
		name + "/tests",
	}
	for _, d := range dirs {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return fmt.Errorf("create dir %s: %w", d, err)
		}
	}

	files := map[string]string{
		name + "/biome.yaml": biomeYAMLTemplate(name),
		name + "/cloud-init/user-data.yaml": `#cloud-config
# TODO: add cloud-init user-data for biome ` + name + `
runcmd:
  - echo "biome ` + name + ` started"
`,
		name + "/cloud-init/network-config.yaml": `version: 2
ethernets:
  eth0:
    dhcp4: true
`,
		name + "/lxd-profile.yaml": `name: ` + name + `
config:
  limits.cpu: "2"
  limits.memory: "2GB"
  security.nesting: "false"
  security.privileged: "false"
devices:
  root:
    path: /
    pool: default
    type: disk
`,
		name + "/image-ref.txt": "ubuntu:24.04\n",
		name + "/hooks/post-deploy.sh": `#!/bin/bash
# post-deploy hook for biome ` + name + `
# Called after the biome instance reaches ready state.
set -euo pipefail
echo "[biome:` + name + `] post-deploy hook complete"
`,
		name + "/tests/smoke.sh": `#!/bin/bash
# Smoke test for biome ` + name + `
# $LXD_INSTANCE, $NODE_IPV4, $NODE_IPV6, $BIOME_VERSION are available.
set -euo pipefail
echo "[smoke:` + name + `] starting"
# TODO: add readiness checks here, e.g.:
# lxc exec "$LXD_INSTANCE" -- curl -sf http://localhost:8080/healthz
echo "[smoke:` + name + `] passed"
`,
	}

	for path, content := range files {
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			return fmt.Errorf("write %s: %w", path, err)
		}
	}

	// Make hooks/tests executable.
	for _, f := range []string{name + "/hooks/post-deploy.sh", name + "/tests/smoke.sh"} {
		_ = os.Chmod(f, 0o755)
	}

	fmt.Printf("Biome scaffold created in ./%s/\n", name)
	fmt.Printf("Edit %s/biome.yaml to declare requirements and readiness probe.\n", name)
	return nil
}

func biomeYAMLTemplate(name string) string {
	return `name: ` + name + `
version: 0.1.0
biome_kind: application     # infrastructure | application | system | utility
phase: post_deploy          # pre_deploy | post_deploy
workload_type: lxc          # lxc | vm | k8s-helm | k8s-manifest
lock_to_host: false
upgrade_strategy: rolling   # rolling | recreate | blue_green

description: |
  ` + name + ` biome — TODO: add description.

requires_hardware_tags: []
#  - "tpm:2.0"
#  - "gpu:nvidia"

dependencies: []
#  - nest-agent

readiness_probe:
  httpGet:
    path: /healthz
    port: 8080
    scheme: HTTP
  initialDelaySeconds: 10
  periodSeconds: 2
  failureThreshold: 30
  successThreshold: 1
  timeoutSeconds: 5

resources:
  cpu: "2"
  memory: "2GB"
  disk: "10GB"
`
}

func biomeValidateCmd() *cobra.Command {
	var runTests bool

	cmd := &cobra.Command{
		Use:   "validate",
		Short: "Validate the biome in the current directory",
		Long: `Run biome validation phases:
  Static   — YAML schema, cloud-init safety, dependency cycle check, readiness probe schema
  Local    — smoke test in an ephemeral LXD instance (--test)
  Integration — full Phase-2 deploy via gough dev node-sim (--test)`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.ValidateBiome(ctx, runTests)
			if err != nil {
				if apiErr, ok := err.(*client.APIError); ok && apiErr.ExitCode() == 6 {
					w.Errorf("Validation failed: %s", apiErr.Message)
					osExit(6)
				}
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Biome validation passed")
			return nil
		},
	}

	cmd.Flags().BoolVar(&runTests, "test", false, "Run local and integration test phases (requires dev primary)")
	return cmd
}

func biomePublishCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "publish",
		Short: "Sign, generate SBOM, and push the biome to the catalog",
		Long: `Publish the biome in the current directory.

  1. Runs all three validation phases (static, local, integration)
  2. Signs with cosign keyless OIDC
  3. Generates an SPDX SBOM via syft
  4. Pushes the OCI artifact to the cluster's Nest mirror
  5. Upserts the catalog entry`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.PublishBiome(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Biome published successfully")
			return nil
		},
	}
}

func biomePromoteCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "promote <name> <version>",
		Short: "Promote a biome version to stable",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.PromoteBiome(ctx, args[0], args[1])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Biome %s@%s promoted to stable", args[0], args[1])
			return nil
		},
	}
}

func biomeRollbackCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "rollback <name>",
		Short: "Roll back a biome to the previous stable version",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.RollbackBiome(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Biome %s rolled back", args[0])
			return nil
		},
	}
}

func biomeDiffCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "diff <name> <v1> <v2>",
		Short: "Show cloud-init diff between two biome versions",
		Args:  cobra.ExactArgs(3),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.DiffBiomes(ctx, args[0], args[1], args[2])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.PrintObject(resp.Data)
			return nil
		},
	}
}

func biomeReSignCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "re-sign <name> <version>",
		Short: "Re-sign an existing biome version with the current signing key",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.ReSignBiome(ctx, args[0], args[1])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Biome %s@%s re-signed", args[0], args[1])
			return nil
		},
	}
}

func biomeEligibilityCheckCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "eligibility-check <biome-instance-id>",
		Short: "Check if a biome instance is eligible for migration or upgrade",
		Long: `Check the migration and upgrade eligibility of a biome instance.

Returns exit code 8 if the safety envelope would reject the operation.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.BiomeEligibilityCheck(ctx, args[0])
			if err != nil {
				if apiErr, ok := err.(*client.APIError); ok && apiErr.ExitCode() == 8 {
					w.Errorf("Safety envelope rejected: %s", apiErr.Message)
					osExit(8)
				}
				return handleAPIError(err, w)
			}
			w.PrintObject(resp.Data)
			return nil
		},
	}
}

func biomeUpgradeCmd() *cobra.Command {
	var toVersion string

	cmd := &cobra.Command{
		Use:   "upgrade <name>",
		Short: "Trigger a rolling upgrade of all instances of a biome",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.UpgradeBiome(ctx, args[0], toVersion)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Rolling upgrade of %s to %s initiated", args[0], toVersion)
			return nil
		},
	}

	cmd.Flags().StringVar(&toVersion, "to", "", "Target version (required)")
	_ = cmd.MarkFlagRequired("to")
	return cmd
}

func biomeDeployCmd() *cobra.Command {
	var (
		node              string
		frontendMode      string
		frontendEndpoint  string
		frontendInterface string
		frontendBaseline  string
	)

	cmd := &cobra.Command{
		Use:   "deploy <name>",
		Short: "Deploy a biome to a node",
		Long: `Deploy a biome to a node.

For k8s-primary biome, control-plane frontend options are available:
  --frontend-mode: kube-vip (default), external, or none
  --frontend-endpoint: Required for kube-vip and external modes
  --frontend-interface: kube-vip mode only; default is primary NIC
  --frontend-baseline: kube-vip mode only; mgmt, internal, or external (default)`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			biomeName := args[0]

			// Validate frontend flags
			if frontendMode == "" {
				frontendMode = "kube-vip"
			}

			// Validate mode value
			validModes := map[string]bool{"kube-vip": true, "external": true, "none": true}
			if !validModes[frontendMode] {
				return fmt.Errorf("invalid --frontend-mode %q; must be kube-vip, external, or none", frontendMode)
			}

			// Endpoint required for kube-vip and external modes
			if (frontendMode == "kube-vip" || frontendMode == "external") && frontendEndpoint == "" {
				return fmt.Errorf("--frontend-endpoint is required for mode=%q", frontendMode)
			}

			// Endpoint must NOT be set for none mode
			if frontendMode == "none" && frontendEndpoint != "" {
				return fmt.Errorf("--frontend-endpoint must not be set when mode=none")
			}

			// Build params map for control_plane_frontend
			params := map[string]string{
				"mode": frontendMode,
			}

			if frontendMode != "none" {
				params["endpoint"] = frontendEndpoint
			}

			if frontendMode == "kube-vip" {
				if frontendInterface != "" {
					params["interface"] = frontendInterface
				}
				if frontendBaseline == "" {
					frontendBaseline = "external"
				}
				params["network_baseline"] = frontendBaseline
			}

			resp, err := c.DeployBiome(ctx, client.DeployBiomeParams{
				BiomeName: biomeName,
				NodeID:    node,
				Params:    params,
			})
			if err != nil {
				return handleAPIError(err, w)
			}

			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}

			w.Successf("Biome %s deployment initiated on node %s", biomeName, node)
			return nil
		},
	}

	cmd.Flags().StringVar(&node, "node", "", "Target node ID (required)")
	_ = cmd.MarkFlagRequired("node")
	cmd.Flags().StringVar(&frontendMode, "frontend-mode", "kube-vip", "Frontend mode: kube-vip, external, or none")
	cmd.Flags().StringVar(&frontendEndpoint, "frontend-endpoint", "", "Frontend endpoint (e.g., 10.2.0.10:6443)")
	cmd.Flags().StringVar(&frontendInterface, "frontend-interface", "", "kube-vip mode only; NIC name (default: primary)")
	cmd.Flags().StringVar(&frontendBaseline, "frontend-baseline", "", "kube-vip mode only; network baseline (mgmt, internal, external)")
	return cmd
}
