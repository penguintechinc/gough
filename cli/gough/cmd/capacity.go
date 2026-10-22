//go:build noxdp

package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

func newCapacityCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "capacity",
		Short: "Capacity forecasting and risk analysis",
	}

	cmd.AddCommand(
		capacityForecastCmd(),
		capacityRisksCmd(),
	)
	return cmd
}

func capacityForecastCmd() *cobra.Command {
	var horizonDays int

	cmd := &cobra.Command{
		Use:   "forecast",
		Short: "Show capacity forecast",
		Long: `Show WaddleAI-driven capacity forecast for the cluster.

Produces per-node projected CPU/memory/disk utilization and breach risk
over the chosen horizon.  Pipe with -o json for scripting.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			fc, err := c.GetCapacityForecast(ctx, horizonDays)
			if err != nil {
				return handleAPIError(err, w)
			}

			rows := make([][]string, 0, len(fc.Nodes))
			for _, n := range fc.Nodes {
				breach := "-"
				if n.BreachInDays > 0 {
					breach = fmt.Sprintf("%.1f days", n.BreachInDays)
				}
				rows = append(rows, []string{
					n.NodeID, n.Hostname,
					fmt.Sprintf("%.1f%%", n.CPUUsedPct),
					fmt.Sprintf("%.1f%%", n.MemUsedPct),
					fmt.Sprintf("%.1f%%", n.ProjCPUPct),
					fmt.Sprintf("%.1f%%", n.ProjMemPct),
					n.BreachRisk, breach,
				})
			}
			w.PrintTable(
				[]string{"NODE", "HOSTNAME", "CPU%", "MEM%", "PROJ_CPU%", "PROJ_MEM%", "RISK", "BREACH_IN"},
				rows,
			)
			return nil
		},
	}

	cmd.Flags().IntVar(&horizonDays, "horizon-days", 7, "Forecast horizon in days (1, 7, or 30)")
	return cmd
}

func capacityRisksCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "risks",
		Short: "Show identified capacity risk alerts",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.GetCapacityRisks(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}

			w.PrintObject(resp.Data)
			return nil
		},
	}
}
