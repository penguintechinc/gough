//go:build noxdp

package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newDeploymentCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "deployment",
		Short: "Manage biome deployments",
		Long:  "List, inspect logs, and cancel biome deployments.",
	}

	cmd.AddCommand(
		deploymentListCmd(),
		deploymentLogsCmd(),
		deploymentCancelCmd(),
	)
	return cmd
}

func deploymentListCmd() *cobra.Command {
	var (
		status  string
		biomeID string
		nodeID  string
	)

	cmd := &cobra.Command{
		Use:   "list",
		Short: "List deployments",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			deployments, err := c.ListDeployments(ctx, client.DeploymentListParams{
				Status:  status,
				BiomeID: biomeID,
				NodeID:  nodeID,
			})
			if err != nil {
				return handleAPIError(err, w)
			}

			if len(deployments) == 0 {
				w.Infof("No deployments found")
				return nil
			}

			rows := make([][]string, 0, len(deployments))
			for _, d := range deployments {
				rows = append(rows, []string{
					d.ID, d.BiomeName, fmt.Sprint(d.NodeID), d.Status,
					fmt.Sprint(d.Phase), d.CreatedAt,
				})
			}
			w.PrintTable(
				[]string{"ID", "BIOME", "NODE", "STATUS", "PHASE", "CREATED"},
				rows,
			)
			return nil
		},
	}

	cmd.Flags().StringVar(&status, "status", "", "Filter by status (pending, in_progress, succeeded, failed, cancelled)")
	cmd.Flags().StringVar(&biomeID, "biome-id", "", "Filter by biome ID")
	cmd.Flags().StringVar(&nodeID, "node-id", "", "Filter by node ID")
	return cmd
}

func deploymentLogsCmd() *cobra.Command {
	var tail int

	cmd := &cobra.Command{
		Use:   "logs <deployment-id>",
		Short: "Show deployment logs",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			logs, err := c.GetDeploymentLogs(ctx, args[0], tail)
			if err != nil {
				return handleAPIError(err, w)
			}

			if len(logs) == 0 {
				w.Infof("No logs found for deployment %s", args[0])
				return nil
			}

			for _, entry := range logs {
				w.Infof("[%s] %s  %s", entry.CreatedAt, entry.Level, entry.Message)
			}
			return nil
		},
	}

	cmd.Flags().IntVar(&tail, "tail", 100, "Number of log lines to return")
	return cmd
}

func deploymentCancelCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "cancel <deployment-id>",
		Short: "Cancel a pending or in-progress deployment",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			_, err = c.CancelDeployment(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}

			w.Successf("Deployment %s cancelled", args[0])
			return nil
		},
	}
}
