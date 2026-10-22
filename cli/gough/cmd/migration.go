//go:build noxdp

package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newMigrationCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "migration",
		Short: "Migration policy and manual migration triggers",
	}

	cmd.AddCommand(
		migrationPolicyShowCmd(),
		migrationPolicySetCmd(),
		migrationTriggerCmd(),
	)
	return cmd
}

func migrationPolicyShowCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "policy show",
		Short: "Show the current migration safety policy",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			policy, err := c.GetMigrationPolicy(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}

			w.PrintKV([][2]string{
				{"min_healthy_nodes", fmt.Sprint(policy.MinHealthyNodes)},
				{"max_concurrent_migrations", fmt.Sprint(policy.MaxConcurrentMigrations)},
				{"require_target_capacity_headroom_mem_pct", fmt.Sprint(policy.RequireTargetCapacityHeadroomMemPct)},
				{"require_target_capacity_headroom_cpu_pct", fmt.Sprint(policy.RequireTargetCapacityHeadroomCPUPct)},
				{"rollback_on_destination_failure", fmt.Sprint(policy.RollbackOnDestinationFailure)},
			})
			return nil
		},
	}
}

func migrationPolicySetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "policy set <field> <value>",
		Short: "Update a migration policy field",
		Long: `Update a single migration safety policy field.

Fields:
  min_healthy_nodes                        — minimum healthy nodes before migration allowed
  max_concurrent_migrations               — maximum simultaneous migrations
  require_target_capacity_headroom_mem_pct — % free memory required on destination
  require_target_capacity_headroom_cpu_pct — % free CPU required on destination
  rollback_on_destination_failure          — true/false`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.SetMigrationPolicy(ctx, args[0], args[1])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Migration policy updated: %s = %s", args[0], args[1])
			return nil
		},
	}
}

func migrationTriggerCmd() *cobra.Command {
	var (
		targetNode string
		ignoreLock bool
		reason     string
	)

	cmd := &cobra.Command{
		Use:   "trigger <biome-instance-id>",
		Short: "Manually trigger migration of a biome instance",
		Long: `Trigger manual live migration of a biome instance.

The migration is pre-checked against the safety envelope (min_healthy_nodes,
max_concurrent_migrations, headroom).  Failure returns exit code 8.

--ignore-lock allows migrating lock_to_host=true biomes; requires --reason.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.TriggerMigration(ctx, args[0], targetNode, ignoreLock, reason)
			if err != nil {
				if apiErr, ok := err.(*client.APIError); ok && apiErr.ExitCode() == 8 {
					w.Errorf("Safety envelope rejected: %s", apiErr.Message)
					return nil
				}
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Migration of %s initiated", args[0])
			return nil
		},
	}

	cmd.Flags().StringVar(&targetNode, "target", "", "Target node ID (optional; auto-selected if omitted)")
	cmd.Flags().BoolVar(&ignoreLock, "ignore-lock", false, "Allow migrating lock_to_host=true biomes (requires --reason)")
	cmd.Flags().StringVar(&reason, "reason", "", "Reason for override (required when --ignore-lock)")
	return cmd
}
