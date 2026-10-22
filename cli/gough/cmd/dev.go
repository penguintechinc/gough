//go:build noxdp

package cmd

import (
	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newDevCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "dev",
		Short: "Developer tools (node simulator, seed data, reset)",
		Long: `Developer and CI tooling for Gough.

These commands require a dev-tier primary and should not be used against
production clusters.`,
	}

	cmd.AddCommand(
		devNodeSimCmd(),
		devSeedCmd(),
		devResetCmd(),
	)
	return cmd
}

func devNodeSimCmd() *cobra.Command {
	var (
		arch     string
		firmware string
		ipv6     bool
	)

	cmd := &cobra.Command{
		Use:   "node-sim",
		Short: "Start a QEMU-based fake node (dev/CI only)",
		Long: `Start a QEMU-based simulated node that PXE-boots against the local dev primary.

The simulator exercises the full Phase-1/2 provisioning flow without
physical hardware.  Supports:
  --arch amd64|arm64
  --firmware bios|uefi
  --ipv6 for DHCPv6/SLAAC path testing`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.StartNodeSim(ctx, client.NodeSimParams{
				Arch:     arch,
				Firmware: firmware,
				IPv6:     ipv6,
			})
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Node simulator started (arch=%s firmware=%s ipv6=%v)", arch, firmware, ipv6)
			return nil
		},
	}

	cmd.Flags().StringVar(&arch, "arch", "amd64", "Simulated node architecture (amd64|arm64)")
	cmd.Flags().StringVar(&firmware, "firmware", "uefi", "Firmware type (bios|uefi)")
	cmd.Flags().BoolVar(&ipv6, "ipv6", false, "Enable IPv6-only networking on the simulated node")
	return cmd
}

func devSeedCmd() *cobra.Command {
	var biomes string

	cmd := &cobra.Command{
		Use:   "seed",
		Short: "Seed a dev primary with the specified biomes",
		Long: `Seed a dev primary with the specified biomes via the dev API.

Biomes are deployed to the first available simulated node.

Example: gough dev seed --biomes k8s-primary,nest-agent,tobogganing`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.DevSeed(ctx, biomes)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Dev seed initiated for biomes: %s", biomes)
			return nil
		},
	}

	cmd.Flags().StringVar(&biomes, "biomes", "", "Comma-separated list of biome names to deploy")
	_ = cmd.MarkFlagRequired("biomes")
	return cmd
}

func devResetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "reset",
		Short: "Reset the dev cluster to a clean state",
		Long: `Reset the dev cluster to a clean state.

Removes all sim nodes, biome instances, and test data.  Does NOT remove
the dev primary LXD containers or Vault state.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.DevReset(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Dev cluster reset")
			return nil
		},
	}
}
