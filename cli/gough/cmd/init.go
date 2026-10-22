//go:build noxdp

package cmd

import (
	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newInitCmd() *cobra.Command {
	var (
		ha                bool
		dhcpAuthoritative bool
		restoreFrom       string
	)

	cmd := &cobra.Command{
		Use:   "init",
		Short: "Bootstrap a Gough primary node (Phase 0)",
		Long: `Bootstrap a Gough primary node on the local host.

gough init is idempotent — it inspects current state and converges only diffs.
It never destroys existing configuration without --reinit.

This command:
  • Detects bare-metal vs cloud-VM host context
  • Installs the LXD snap and initializes the cluster
  • Bootstraps SPIRE and Vault
  • Starts api-manager, webui, and worker-ipxe as LXD containers
  • Seeds the biome catalog
  • Probes the LAN for an existing DHCP server (proxy vs authoritative mode)

After init, run 'gough login --cluster https://<primary-ip>' to authenticate.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.Init(ctx, client.InitParams{
				HA:                ha,
				DHCPAuthoritative: dhcpAuthoritative,
				RestoreFrom:       restoreFrom,
			})
			if err != nil {
				return handleAPIError(err, w)
			}

			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}

			w.Successf("Primary bootstrap initiated; watch progress via 'gough cluster status'")
			return nil
		},
	}

	cmd.Flags().BoolVar(&ha, "ha", false, "Bootstrap in HA mode (3-node quorum)")
	cmd.Flags().BoolVar(&dhcpAuthoritative, "dhcp-authoritative", false,
		"Force authoritative DHCP mode (default: auto-detect; proxy if existing server found)")
	cmd.Flags().StringVar(&restoreFrom, "restore-from", "",
		"Restore from an S3 backup URL instead of fresh bootstrap")

	return cmd
}
