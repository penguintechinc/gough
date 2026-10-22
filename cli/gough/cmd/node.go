//go:build noxdp

package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newNodeCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "node",
		Short: "Manage cluster nodes",
		Long:  "Discover, deploy, reject, decommission, evacuate, and rekey cluster nodes.",
	}

	cmd.AddCommand(
		nodeListCmd(),
		nodeShowCmd(),
		nodeDeployCmd(),
		nodeRejectCmd(),
		nodeDecommissionCmd(),
		nodeEvacuateCmd(),
		nodeRekeyCmd(),
		nodeTagCmd(),
		nodeTagsCmd(),
	)
	return cmd
}

func nodeListCmd() *cobra.Command {
	var (
		state    string
		tenantID string
		tag      string
	)

	cmd := &cobra.Command{
		Use:   "list",
		Short: "List cluster nodes",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			nodes, err := c.ListNodes(ctx, client.NodeListParams{
				State:  state,
				Tenant: tenantID,
				Tag:    tag,
			})
			if err != nil {
				return handleAPIError(err, w)
			}

			if len(nodes) == 0 {
				w.Infof("No nodes found")
				return nil
			}

			rows := make([][]string, 0, len(nodes))
			for _, n := range nodes {
				memGB := fmt.Sprintf("%d GB", n.MemoryMB/1024)
				tagStr := formatTags(n.Tags)
				rows = append(rows, []string{
					n.ID, n.Hostname, n.State, n.Arch,
					fmt.Sprint(n.CPUCount), memGB, n.TenantID, tagStr,
				})
			}
			w.PrintTable(
				[]string{"ID", "HOSTNAME", "STATE", "ARCH", "CPU", "MEM", "TENANT", "TAGS"},
				rows,
			)
			return nil
		},
	}

	cmd.Flags().StringVar(&state, "state", "", "Filter by node state (new|probed|planned|deploying|configuring|ready|upgrading|quarantined|draining|decommissioned|rejected)")
	cmd.Flags().StringVar(&tenantID, "tenant", "", "Filter by tenant ID")
	cmd.Flags().StringVar(&tag, "tag", "", "Filter by tag (key=value)")
	return cmd
}

// formatTags converts a tag map to a comma-separated key:value string.
func formatTags(tags map[string]string) string {
	if len(tags) == 0 {
		return ""
	}
	parts := make([]string, 0, len(tags))
	for k, v := range tags {
		parts = append(parts, k+":"+v)
	}
	return strings.Join(parts, ",")
}

// nodeTagCmd returns a "tag" sub-command group with add/remove subcommands.
func nodeTagCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "tag",
		Short: "Manage node tags",
	}
	cmd.AddCommand(nodeTagAddCmd(), nodeTagRemoveCmd())
	return cmd
}

func nodeTagAddCmd() *cobra.Command {
	return &cobra.Command{
		Use:     "add <node-id> <key>=<value>",
		Short:   "Add a tag to a node",
		Example: "  gough node tag add dal2-w03 gpu=nvidia",
		Args:    cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			nodeID := args[0]
			kv := args[1]
			parts := strings.SplitN(kv, "=", 2)
			if len(parts) != 2 {
				return fmt.Errorf("tag must be in key=value format, got %q", kv)
			}
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.AddNodeTag(ctx, nodeID, parts[0], parts[1])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Tag %s added to node %s", kv, nodeID)
			return nil
		},
	}
}

func nodeTagRemoveCmd() *cobra.Command {
	return &cobra.Command{
		Use:     "remove <node-id> <key>=<value>",
		Short:   "Remove a tag from a node",
		Example: "  gough node tag remove dal2-w03 gpu=nvidia",
		Args:    cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			nodeID := args[0]
			kv := args[1]
			parts := strings.SplitN(kv, "=", 2)
			if len(parts) != 2 {
				return fmt.Errorf("tag must be in key=value format, got %q", kv)
			}
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.RemoveNodeTag(ctx, nodeID, parts[0], parts[1])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Tag %s removed from node %s", kv, nodeID)
			return nil
		},
	}
}

func nodeTagsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "tags <node-id>",
		Short: "List all tags on a node",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			tags, err := c.GetNodeTags(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if len(tags) == 0 {
				w.Infof("No tags on node %s", args[0])
				return nil
			}
			rows := make([][]string, 0, len(tags))
			for k, v := range tags {
				rows = append(rows, []string{k, v})
			}
			w.PrintTable([]string{"KEY", "VALUE"}, rows)
			return nil
		},
	}
}

func nodeShowCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "show <id>",
		Short: "Show full details for a node",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			node, err := c.ShowNode(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}

			w.PrintObject(node)
			return nil
		},
	}
}

func nodeDeployCmd() *cobra.Command {
	var planFile string

	cmd := &cobra.Command{
		Use:   "deploy <id>",
		Short: "Deploy a probed node using a plan file",
		Long: `Compile and execute a node deployment plan.

The plan file is a YAML document specifying disk layout and biome set.
If --plan is omitted, the previously compiled plan is used.`,
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
					// Try as a raw bytes plan (YAML passed through as-is).
					plan = map[string]string{"raw": string(raw)}
				}
			}

			resp, err := c.DeployNode(ctx, args[0], plan)
			if err != nil {
				if apiErr, ok := err.(*client.APIError); ok && apiErr.ExitCode() == 7 {
					w.Errorf("Plan compilation failed: %s", apiErr.Message)
					osExit(7)
				}
				return handleAPIError(err, w)
			}

			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}

			w.Successf("Deployment initiated for node %s", args[0])
			return nil
		},
	}

	cmd.Flags().StringVar(&planFile, "plan", "", "Path to plan YAML/JSON file")
	return cmd
}

func nodeRejectCmd() *cobra.Command {
	var reason string

	cmd := &cobra.Command{
		Use:   "reject <id>",
		Short: "Reject a probed node",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.RejectNode(ctx, args[0], reason)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Node %s rejected", args[0])
			return nil
		},
	}

	cmd.Flags().StringVar(&reason, "reason", "", "Reason for rejection (audit-logged)")
	_ = cmd.MarkFlagRequired("reason")
	return cmd
}

func nodeDecommissionCmd() *cobra.Command {
	var reason string

	cmd := &cobra.Command{
		Use:   "decommission <id>",
		Short: "Decommission a node (drains workloads first)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.DecommissionNode(ctx, args[0], reason)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Decommission initiated for node %s", args[0])
			return nil
		},
	}

	cmd.Flags().StringVar(&reason, "reason", "", "Reason for decommission (audit-logged)")
	_ = cmd.MarkFlagRequired("reason")
	return cmd
}

func nodeEvacuateCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "evacuate <id>",
		Short: "Evacuate (migrate) all movable biomes off a node",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.EvacuateNode(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Evacuation initiated for node %s", args[0])
			return nil
		},
	}
}

func nodeRekeyCmd() *cobra.Command {
	var reason string

	cmd := &cobra.Command{
		Use:   "rekey <id>",
		Short: "Rotate the LUKS encryption key for a node",
		Long: `Rotate the LUKS disk encryption key for a deployed node.

The node enters a transient 'rekeying' state during rotation.  The node
remains functional but is blocked from drains and upgrades until complete.

Reason must be one of: rotation, compromise, compliance.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.RekeyNode(ctx, args[0], reason)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("LUKS rekey initiated for node %s (reason: %s)", args[0], reason)
			return nil
		},
	}

	cmd.Flags().StringVar(&reason, "reason", "", "Rekey reason: rotation|compromise|compliance")
	_ = cmd.MarkFlagRequired("reason")
	return cmd
}
