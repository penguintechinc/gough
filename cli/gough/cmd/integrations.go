//go:build noxdp

package cmd

import (
	"strings"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newIntegrationsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "integrations",
		Short: "Manage external integrations (Squawk, Skauswatch, Tobogganing, WaddleAI, etc.)",
	}

	cmd.AddCommand(
		integrationsStatusCmd(),
		integrationsConfigureCmd(),
		integrationsRotateCredentialsCmd(),
		integrationsValidateScopeCmd(),
	)
	return cmd
}

func integrationsStatusCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "status",
		Short: "Show status of all configured integrations",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			integrations, err := c.ListIntegrations(ctx)
			if err != nil {
				return handleAPIError(err, w)
			}

			if len(integrations) == 0 {
				w.Infof("No integrations configured")
				return nil
			}

			rows := make([][]string, 0, len(integrations))
			for _, i := range integrations {
				scopeOK := "yes"
				if !i.ScopeOK {
					scopeOK = "no"
				}
				rows = append(rows, []string{
					i.Name, i.Status, i.Version, i.Endpoint, i.LastPing, scopeOK,
				})
			}
			w.PrintTable(
				[]string{"NAME", "STATUS", "VERSION", "ENDPOINT", "LAST_PING", "SCOPE_OK"},
				rows,
			)
			return nil
		},
	}
}

func integrationsConfigureCmd() *cobra.Command {
	var opts []string

	cmd := &cobra.Command{
		Use:   "configure <name>",
		Short: "Configure a named integration",
		Long: `Configure a named integration.

Example:
  gough integrations configure waddleai --opt endpoint=https://waddleai.example.com
  gough integrations configure squawk --opt endpoint=https://squawk.example.com --opt zone=gough.local`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			cfg := make(map[string]string)
			for _, o := range opts {
				parts := strings.SplitN(o, "=", 2)
				if len(parts) == 2 {
					cfg[parts[0]] = parts[1]
				}
			}

			resp, err := c.ConfigureIntegration(ctx, args[0], cfg)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Integration %s configured", args[0])
			return nil
		},
	}

	cmd.Flags().StringArrayVar(&opts, "opt", nil, "Configuration option as key=value (repeatable)")
	return cmd
}

func integrationsRotateCredentialsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "rotate-credentials <name>",
		Short: "Rotate credentials for a named integration",
		Long: `Rotate the service-account credentials used by a named integration.

New credentials are issued by the target product and stored in Vault.
The rotation is audit-logged.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.RotateIntegrationCredentials(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Credentials rotated for integration %s", args[0])
			return nil
		},
	}
}

func integrationsValidateScopeCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "validate-scope <name>",
		Short: "Validate that the integration service account has the required scopes",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.ValidateIntegrationScope(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.PrintObject(resp.Data)
			return nil
		},
	}
}
