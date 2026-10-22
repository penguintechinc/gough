//go:build noxdp

package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/config"
)

func newConfigCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "config",
		Short: "Manage gough CLI configuration",
		Long: `Manage gough CLI configuration stored in:
  Linux/macOS: $XDG_CONFIG_HOME/gough/config.yaml (default ~/.config/gough/config.yaml)
  Windows:     %APPDATA%\gough\config.yaml`,
	}

	cmd.AddCommand(
		configGetCmd(),
		configSetCmd(),
		configListCmd(),
		configGetContextsCmd(),
		configCurrentContextCmd(),
		configUseContextCmd(),
		configSetContextCmd(),
		configDeleteContextCmd(),
	)
	return cmd
}

func configGetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "get <key>",
		Short: "Get a configuration value",
		Example: `  gough config get cluster.url
  gough config get current_context`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()
			cfg, err := config.New()
			if err != nil {
				return err
			}
			val := cfg.Get(args[0])
			if val == "" {
				w.Infof("(not set)")
				return nil
			}
			w.Infof("%s", val)
			return nil
		},
	}
}

func configSetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "set <key> <value>",
		Short: "Set a configuration value",
		Example: `  gough config set cluster.url https://gough.example.com
  gough config set current_context production`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()
			cfg, err := config.New()
			if err != nil {
				return err
			}
			if err := cfg.Set(args[0], args[1]); err != nil {
				return fmt.Errorf("set %s: %w", args[0], err)
			}
			w.Successf("Set %s = %s", args[0], args[1])
			return nil
		},
	}
}

func configListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List all configuration values",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()
			cfg, err := config.New()
			if err != nil {
				return err
			}
			all := cfg.All()
			if len(all) == 0 {
				w.Infof("(no configuration set)")
				return nil
			}
			path, _ := config.Path()
			w.Infof("Configuration file: %s\n", path)
			rows := make([][]string, 0, len(all))
			for k, v := range all {
				rows = append(rows, []string{k, fmt.Sprintf("%v", v)})
			}
			w.PrintTable([]string{"KEY", "VALUE"}, rows)
			return nil
		},
	}
}

func configGetContextsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "get-contexts",
		Short: "List all defined contexts",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()
			cfg, err := config.New()
			if err != nil {
				return err
			}
			contexts := cfg.Contexts()
			if len(contexts) == 0 {
				w.Infof("(no contexts defined)")
				return nil
			}
			currentCtx := cfg.CurrentContext()
			rows := make([][]string, 0, len(contexts))
			for name, ctx := range contexts {
				active := ""
				if name == currentCtx {
					active = "✓"
				}
				rows = append(rows, []string{name, ctx.ClusterURL, ctx.TenantID, active})
			}
			w.PrintTable([]string{"NAME", "CLUSTER URL", "TENANT ID", "ACTIVE"}, rows)
			return nil
		},
	}
}

func configCurrentContextCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "current-context",
		Short: "Print the active context name",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()
			cfg, err := config.New()
			if err != nil {
				return err
			}
			current := cfg.CurrentContext()
			if current == "" {
				w.Infof("(none)")
				return nil
			}
			w.Infof("%s", current)
			return nil
		},
	}
}

func configUseContextCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "use-context <name>",
		Short: "Switch to a named context",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()
			cfg, err := config.New()
			if err != nil {
				return err
			}
			if err := cfg.UseContext(args[0]); err != nil {
				return err
			}
			w.Successf("Switched to context %q", args[0])
			return nil
		},
	}
}

func configSetContextCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "set-context <name>",
		Short: "Create or update a named context",
		Example: `  gough config set-context production --cluster https://gough.prod.example.com --tenant tenant-123
  gough config set-context staging --cluster https://gough.staging.example.com`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()
			cluster, _ := cmd.Flags().GetString("cluster")
			tenant, _ := cmd.Flags().GetString("tenant")

			if cluster == "" {
				return fmt.Errorf("--cluster is required")
			}

			cfg, err := config.New()
			if err != nil {
				return err
			}
			ctx := config.Context{
				ClusterURL: cluster,
				TenantID:   tenant,
			}
			if err := cfg.SetContext(args[0], ctx); err != nil {
				return err
			}
			w.Successf("Created/updated context %q", args[0])
			return nil
		},
	}
	cmd.Flags().StringP("cluster", "c", "", "Cluster URL (required)")
	cmd.Flags().StringP("tenant", "t", "", "Tenant ID (optional)")
	return cmd
}

func configDeleteContextCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "delete-context <name>",
		Short: "Remove a named context",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()
			cfg, err := config.New()
			if err != nil {
				return err
			}
			if err := cfg.DeleteContext(args[0]); err != nil {
				return err
			}
			w.Successf("Deleted context %q", args[0])
			return nil
		},
	}
}
