//go:build noxdp

package cmd

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newClusterCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "cluster",
		Short: "Cluster lifecycle operations",
	}

	cmd.AddCommand(
		clusterStatusCmd(),
		clusterUpgradeCmd(),
		clusterEvacuateCmd(),
		clusterAdoptCmd(),
		clusterRotateJoinerSecretsCmd(),
		clusterIdentityPlaneCmd(),
		clusterNetworkBaselineCmd(),
		clusterTagVocabularyCmd(),
	)
	return cmd
}

// newLXDCmd returns the top-level lxd command group.
func newLXDCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "lxd",
		Short: "LXD cluster operations",
	}
	cmd.AddCommand(
		lxdShowTrustPasswordCmd(),
		lxdRotateTrustPasswordCmd(),
	)
	return cmd
}

func clusterIdentityPlaneCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "identity-plane",
		Short: "Identity plane (SPIRE) operations",
	}
	cmd.AddCommand(
		clusterIdentityPlaneStatusCmd(),
		clusterIdentityPlaneConfigureCmd(),
	)
	return cmd
}

func clusterIdentityPlaneStatusCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "status",
		Short: "Show identity plane status",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.IdentityPlaneStatus(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}
			w.PrintObject(resp.Data)
			return nil
		},
	}
}

func clusterIdentityPlaneConfigureCmd() *cobra.Command {
	var provider string

	cmd := &cobra.Command{
		Use:   "configure",
		Short: "Configure the identity plane provider",
		Long: `Configure the identity plane provider.

Providers:
  builtin    — minimal built-in SPIRE (bootstrap/dev only)
  skauswatch — PenguinTech Skauswatch identity platform (recommended)
  external   — bring your own SPIRE deployment`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.IdentityPlaneConfigure(ctx, provider)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Identity plane configured: provider=%s", provider)
			return nil
		},
	}
	cmd.Flags().StringVar(&provider, "provider", "", "Provider: builtin|skauswatch|external")
	_ = cmd.MarkFlagRequired("provider")
	return cmd
}

func clusterNetworkBaselineCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "network-baseline",
		Short: "Network baseline configuration",
	}
	cmd.AddCommand(
		clusterNetworkBaselineStatusCmd(),
		clusterNetworkBaselineConfigureCmd(),
		clusterNetworkBaselineMigrateCmd(),
	)
	return cmd
}

func clusterNetworkBaselineStatusCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "status",
		Short: "Show network baseline status",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.NetworkBaselineStatus(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}
			w.PrintObject(resp.Data)
			return nil
		},
	}
}

func clusterNetworkBaselineConfigureCmd() *cobra.Command {
	var opts []string

	cmd := &cobra.Command{
		Use:   "configure <baseline>",
		Short: "Configure a network baseline",
		Long: `Configure a network baseline for the cluster.

Baselines: mgmt, internal, external

Example:
  gough cluster network-baseline configure mgmt --opt cidr=10.0.0.0/24
  gough cluster network-baseline configure external --opt vlan=100`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			optMap := make(map[string]string)
			for _, o := range opts {
				parts := strings.SplitN(o, "=", 2)
				if len(parts) == 2 {
					optMap[parts[0]] = parts[1]
				}
			}
			resp, err := c.NetworkBaselineConfigure(ctx, args[0], optMap)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Network baseline %s configured", args[0])
			return nil
		},
	}
	cmd.Flags().StringArrayVar(&opts, "opt", nil, "Option as key=value (repeatable)")
	return cmd
}

func clusterNetworkBaselineMigrateCmd() *cobra.Command {
	var to string

	cmd := &cobra.Command{
		Use:   "migrate <baseline>",
		Short: "Migrate a network baseline to a target provider",
		Long: `Migrate a network baseline to Squawk (recommended) or another provider.

Example:
  gough cluster network-baseline migrate internal --to squawk`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.NetworkBaselineMigrate(ctx, args[0], to)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Network baseline %s migration to %s initiated", args[0], to)
			return nil
		},
	}
	cmd.Flags().StringVar(&to, "to", "", "Target provider (e.g. squawk)")
	_ = cmd.MarkFlagRequired("to")
	return cmd
}

func clusterTagVocabularyCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "tag-vocabulary",
		Short: "List the cluster's allowed tag vocabulary",
		Long: `List the allowed tag keys and permitted values defined in the cluster's
tag vocabulary policy.  Nodes may only be tagged with keys and values from
this vocabulary unless unrestricted tagging is enabled.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.GetTagVocabulary(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}
			w.PrintObject(resp.Data)
			return nil
		},
	}
}

func clusterStatusCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "status",
		Short: "Show cluster health and component status",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			status, err := c.GetClusterStatus(ctx)
			if err != nil {
				if apiErr, ok := err.(*client.APIError); ok && apiErr.ExitCode() == 9 {
					w.Errorf("Cluster unhealthy: %s", apiErr.Message)
					osExit(9)
				}
				return handleAPIError(err, w)
			}

			w.PrintKV([][2]string{
				{"cluster_id", status.ClusterID},
				{"state", status.State},
				{"nodes", fmt.Sprintf("%d total, %d ready", status.NodeCount, status.ReadyNodes)},
				{"biome_instances", fmt.Sprint(status.BiomeInstances)},
				{"version", status.Version},
				{"vault_sealed", fmt.Sprint(status.VaultSealed)},
				{"spire_healthy", fmt.Sprint(status.SpireHealthy)},
				{"db_healthy", fmt.Sprint(status.DBHealthy)},
				{"nats_healthy", fmt.Sprint(status.NATSHealthy)},
				{"k8s_healthy", fmt.Sprint(status.K8SHealthy)},
			})
			return nil
		},
	}
}

func clusterUpgradeCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "upgrade",
		Short: "Upgrade cluster services",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.UpgradeCluster(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Cluster upgrade initiated")
			return nil
		},
	}
}

func clusterEvacuateCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "evacuate <node-id>",
		Short: "Evacuate all movable biomes off a cluster node",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.EvacuateCluster(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Evacuation of node %s initiated", args[0])
			return nil
		},
	}
}

func clusterAdoptCmd() *cobra.Command {
	var extraFlags []string

	cmd := &cobra.Command{
		Use:   "adopt {k8s|lxd|ceph|longhorn}",
		Short: "Adopt an existing k8s, LXD, Ceph, or Longhorn resource into the cluster",
		Long: `Adopt a brownfield resource into the Gough cluster.

Examples:
  gough cluster adopt k8s --kubeconfig ~/.kube/config
  gough cluster adopt lxd --endpoint https://lxd.example.com:8443
  gough cluster adopt ceph --mon-ips 10.0.0.1,10.0.0.2
  gough cluster adopt longhorn --namespace longhorn-system`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			kind := args[0]
			if kind != "k8s" && kind != "lxd" && kind != "ceph" && kind != "longhorn" {
				return fmt.Errorf("unsupported adopt kind %q; must be k8s, lxd, ceph, or longhorn", kind)
			}

			opts := make(map[string]string)
			for _, f := range extraFlags {
				parts := strings.SplitN(f, "=", 2)
				if len(parts) == 2 {
					opts[parts[0]] = parts[1]
				}
			}

			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.AdoptCluster(ctx, kind, opts)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Adopt %s initiated", kind)
			return nil
		},
	}

	cmd.Flags().StringArrayVar(&extraFlags, "opt", nil, "Extra adopt options as key=value pairs")
	return cmd
}

func clusterRotateJoinerSecretsCmd() *cobra.Command {
	var biomeKind string

	cmd := &cobra.Command{
		Use:   "rotate-joiner-secrets",
		Short: "Rotate joiner secrets (optionally filtered by biome kind)",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.RotateJoinerSecrets(ctx, biomeKind)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Joiner secret rotation initiated")
			return nil
		},
	}

	cmd.Flags().StringVar(&biomeKind, "biome-kind", "", "Rotate secrets only for this biome kind")
	return cmd
}

func lxdShowTrustPasswordCmd() *cobra.Command {
	var reason string

	cmd := &cobra.Command{
		Use:   "show-trust-password",
		Short: "Retrieve the LXD cluster trust password (requires cluster.admin scope; audit-logged)",
		Long: `Retrieve the LXD cluster trust password from Vault.

This action requires cluster.admin scope and is always audit-logged with
the operator's identity and the stated reason.

The password is printed ONCE; it is not stored in the CLI session.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.LXDShowTrustPassword(ctx, reason)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			// Password is in the API response; pass through directly.
			w.PrintObject(resp.Data)
			return nil
		},
	}

	cmd.Flags().StringVar(&reason, "reason", "", "Reason for retrieving the trust password (audit-logged, required)")
	_ = cmd.MarkFlagRequired("reason")
	return cmd
}

func lxdRotateTrustPasswordCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "rotate-trust-password",
		Short: "Rotate the LXD cluster trust password (disruptive; requires maintenance window)",
		Long: `Rotate the LXD cluster trust password.

This is disruptive — it triggers a re-issue to every cluster member and
should run under a maintenance window.  The operation is audit-logged.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}
			resp, err := c.LXDRotateTrustPassword(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("LXD trust password rotation initiated")
			return nil
		},
	}
}
