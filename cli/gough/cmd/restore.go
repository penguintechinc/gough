//go:build noxdp

package cmd

import (
	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

// newRestoreCmd returns the top-level `gough restore` command.
// This is the shorthand form; `gough dr restore` is also available.
func newRestoreCmd() *cobra.Command {
	var (
		from      string
		clusterID string
	)

	cmd := &cobra.Command{
		Use:   "restore",
		Short: "Restore a cluster from an S3 backup",
		Long: `Restore a cluster from an S3 backup URL.

This is equivalent to 'gough dr restore'.  Triggers a full cluster restore
from the specified S3 backup bundle.`,
		Args: cobra.NoArgs,
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
