//go:build noxdp

package cmd

import (
	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newDRCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "dr",
		Short: "Disaster recovery operations (drill, promote, failback, restore)",
	}

	cmd.AddCommand(
		drDrillCmd(),
		drPromoteCmd(),
		drFailbackCmd(),
		drRestoreCmd(),
	)
	return cmd
}

func drDrillCmd() *cobra.Command {
	var target string

	cmd := &cobra.Command{
		Use:   "drill",
		Short: "Run a DR drill",
		Long: `Run a DR drill against a staging clone or the configured DR target.

Exits with code 10 if the drill fails.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.RunDRDrill(ctx, target)
			if err != nil {
				if apiErr, ok := err.(*client.APIError); ok && apiErr.ExitCode() == 10 {
					w.Errorf("DR drill failed: %s", apiErr.Message)
					osExit(10)
				}
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("DR drill initiated")
			return nil
		},
	}

	cmd.Flags().StringVar(&target, "target", "", "Target site ID or 'staging-clone'")
	return cmd
}

func drPromoteCmd() *cobra.Command {
	var (
		reason            string
		sourceUnreachable bool
	)

	cmd := &cobra.Command{
		Use:   "promote <site-id>",
		Short: "Promote a DR site to primary (requires cluster.superadmin co-sign)",
		Long: `Promote a DR site to become the active primary.

This operation:
  1. Promotes the secondary Postgres to primary
  2. Promotes Vault performance standby to leader
  3. Re-anchors SPIRE federation
  4. Verifies biome catalog OCI mirror availability
  5. Updates Squawk DNS for the cutover

Requires cluster-superadmin scope.  The operation is always audit-logged
with the stated reason.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.PromoteDRSite(ctx, args[0], reason, sourceUnreachable)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("DR promotion of site %s initiated", args[0])
			return nil
		},
	}

	cmd.Flags().StringVar(&reason, "reason", "", "Reason for promotion (required, audit-logged)")
	cmd.Flags().BoolVar(&sourceUnreachable, "source-unreachable", false,
		"Assert primary is unreachable (bypasses source health check)")
	_ = cmd.MarkFlagRequired("reason")
	return cmd
}

func drFailbackCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "failback <site-id>",
		Short: "Fail back from DR site to original primary",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.FailbackDRSite(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("DR failback to site %s initiated", args[0])
			return nil
		},
	}
}

func drRestoreCmd() *cobra.Command {
	var (
		from      string
		clusterID string
	)

	cmd := &cobra.Command{
		Use:   "restore",
		Short: "Restore a cluster from an S3 backup",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.RestoreFromBackup(ctx, from, clusterID)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Restore from %s initiated", from)
			return nil
		},
	}

	cmd.Flags().StringVar(&from, "from", "", "S3 URL of the backup (e.g. s3://bucket/path)")
	cmd.Flags().StringVar(&clusterID, "cluster-id", "", "Target cluster ID")
	_ = cmd.MarkFlagRequired("from")
	_ = cmd.MarkFlagRequired("cluster-id")
	return cmd
}
