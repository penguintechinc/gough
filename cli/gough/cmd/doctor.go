//go:build noxdp

package cmd

import (
	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newDoctorCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "doctor",
		Short: "Diagnose cluster and node health",
		Long: `Run diagnostic checks against the cluster and its nodes.

  gough doctor network            — diagnose network topology, MTU, DNS, DHCP
  gough doctor helper-image       — verify helper image integrity and boot chain
  gough doctor encryption-status  — verify LUKS/Vault seal status across nodes
  gough doctor crypto-inventory   — audit cryptographic keys, SVIDs, and TTLs`,
	}

	cmd.AddCommand(
		doctorCheckCmd("network",
			"Diagnose network topology, MTU negotiation, DNS resolution, and DHCP"),
		doctorCheckCmd("helper-image",
			"Verify helper image integrity, iPXE trust anchor, and boot chain"),
		doctorCheckCmd("encryption-status",
			"Check LUKS encryption and Vault seal status across all nodes"),
		doctorCheckCmd("crypto-inventory",
			"Audit SPIRE SVIDs, Vault transit keys, and mTLS certificates by TTL"),
	)
	return cmd
}

func doctorCheckCmd(name, short string) *cobra.Command {
	return &cobra.Command{
		Use:   name,
		Short: short,
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.RunDoctorCheck(ctx, name, map[string]string{})
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
