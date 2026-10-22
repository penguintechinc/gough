//go:build noxdp

package cmd

import (
	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newPrimaryCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "primary",
		Short: "Manage the Gough primary node",
	}

	cmd.AddCommand(
		primaryReplaceCmd(),
		primaryForceRecoverCmd(),
		primaryFrontendSwitchCmd(),
		primaryRotateCACmd(),
	)
	return cmd
}

func primaryReplaceCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "replace <node-id>",
		Short: "Replace the primary node",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.ReplacePrimary(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Primary replacement initiated")
			return nil
		},
	}
}

func primaryForceRecoverCmd() *cobra.Command {
	var survivingNode string
	var reason string

	cmd := &cobra.Command{
		Use:   "force-recover",
		Short: "Force-recover the primary using a surviving node (requires cluster.superadmin scope)",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.ForceRecoverPrimary(ctx, survivingNode, reason)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Force-recovery initiated")
			return nil
		},
	}

	cmd.Flags().StringVar(&survivingNode, "surviving-node", "", "ID of the surviving node to recover from")
	cmd.Flags().StringVar(&reason, "reason", "", "Reason for force recovery (required, audit-logged)")
	_ = cmd.MarkFlagRequired("surviving-node")
	_ = cmd.MarkFlagRequired("reason")
	return cmd
}

func primaryFrontendSwitchCmd() *cobra.Command {
	var mode string

	cmd := &cobra.Command{
		Use:   "frontend-switch",
		Short: "Switch the primary API frontend mode",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.SwitchPrimaryFrontend(ctx, mode)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Frontend switched to mode: %s", mode)
			return nil
		},
	}

	cmd.Flags().StringVar(&mode, "mode", "", "Frontend mode: vip or anycast")
	_ = cmd.MarkFlagRequired("mode")
	return cmd
}

func primaryRotateCACmd() *cobra.Command {
	return &cobra.Command{
		Use:   "rotate-ca",
		Short: "Rotate the Gough internal CA certificate",
		Long: `Rotate the Gough internal CA.  This rebuilds the iPXE binary with the new
trust anchor and triggers CA store updates on all cluster members.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.RotatePrimaryCA(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("CA rotation initiated; iPXE binary rebuild queued")
			return nil
		},
	}
}
