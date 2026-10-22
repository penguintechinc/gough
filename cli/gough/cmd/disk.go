//go:build noxdp

package cmd

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newDiskCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "disk",
		Short: "Manage node disk plans and SMART health",
	}

	cmd.AddCommand(
		diskListCmd(),
		diskPlanCmd(),
		diskSmartRecheckCmd(),
	)
	return cmd
}

func diskListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list <node-id>",
		Short: "List disks on a node",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			disks, err := c.ListDisks(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}

			if len(disks) == 0 {
				w.Infof("No disks found for node %s", args[0])
				return nil
			}

			rows := make([][]string, 0, len(disks))
			for _, d := range disks {
				dark := "no"
				if d.DarkDrive {
					dark = "yes"
				}
				rows = append(rows, []string{
					d.ID, d.Device,
					fmt.Sprintf("%d GB", d.SizeGB),
					d.Kind, dark, d.SmartState, d.Model,
				})
			}
			w.PrintTable(
				[]string{"ID", "DEVICE", "SIZE", "KIND", "DARK", "SMART", "MODEL"},
				rows,
			)
			return nil
		},
	}
}

func diskPlanCmd() *cobra.Command {
	var planFile string

	cmd := &cobra.Command{
		Use:   "plan <node-id>",
		Short: "Apply a disk plan to a node",
		Long: `Apply a disk plan to a probed node.

The plan file is a YAML document specifying which devices to use, which
to mark as dark drives (reserved for distributed storage), and the
partition layout (sgdisk/sfdisk commands).`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			var plan interface{}
			if planFile != "" {
				raw, err := os.ReadFile(planFile)
				if err != nil {
					return fmt.Errorf("read plan file: %w", err)
				}
				if err := json.Unmarshal(raw, &plan); err != nil {
					plan = map[string]string{"raw": string(raw)}
				}
			}

			resp, err := c.PlanDisks(ctx, args[0], plan)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Disk plan applied to node %s", args[0])
			return nil
		},
	}

	cmd.Flags().StringVar(&planFile, "plan", "", "Path to disk plan YAML/JSON file")
	_ = cmd.MarkFlagRequired("plan")
	return cmd
}

func diskSmartRecheckCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "smart-recheck <node-id> <disk-id>",
		Short: "Trigger a SMART re-check for a specific disk",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.SmartRecheck(ctx, args[0], args[1])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("SMART recheck queued for disk %s on node %s", args[1], args[0])
			return nil
		},
	}
}
