//go:build noxdp

package cmd

import (
	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newSyncCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "sync",
		Short: "Manage air-gapped sneakernet bundles",
		Long: `Manage air-gapped content bundles.

  gough sync export <dir>   — produce a sneakernet bundle on an internet-connected primary
  gough sync import <dir>   — apply the bundle to an air-gapped primary

Bundle contents: OCI images, Debian apt mirror, Snap packages, LVFS firmware,
and optionally language package mirrors (PyPI / npm / Go modules).`,
	}

	cmd.AddCommand(
		syncExportCmd(),
		syncImportCmd(),
	)
	return cmd
}

func syncExportCmd() *cobra.Command {
	return &cobra.Command{
		Use:     "export <dir>",
		Short:   "Produce a sneakernet bundle (run on internet-connected primary)",
		Example: "  gough sync export /mnt/usb/gough-bundle-2026-04",
		Args:    cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.SyncExport(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Bundle export started in %s", args[0])
			return nil
		},
	}
}

func syncImportCmd() *cobra.Command {
	return &cobra.Command{
		Use:     "import <dir>",
		Short:   "Apply a sneakernet bundle (run on air-gapped primary)",
		Example: "  gough sync import /mnt/usb/gough-bundle-2026-04",
		Args:    cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.SyncImport(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Bundle import started from %s", args[0])
			return nil
		},
	}
}
