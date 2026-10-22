//go:build noxdp

package cmd

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/auth"
	"github.com/penguintechinc/gough/cli/gough/internal/config"
)

func newLoginCmd() *cobra.Command {
	var clusterURL string

	cmd := &cobra.Command{
		Use:   "login",
		Short: "Authenticate with a Gough cluster using OIDC device-code flow",
		Long: `Authenticate with a Gough cluster via OIDC device-code flow.

The access token is stored in the OS keychain (macOS Keychain, GNOME Keyring /
libsecret on Linux, Windows Credential Manager).  The token is NEVER written to
disk as plaintext and NEVER passed as a CLI argument.

The GOUGH_TOKEN environment variable may be set to bypass the keychain for
automation scenarios (CI/CD).`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()

			cfg, err := config.New()
			if err != nil {
				return fmt.Errorf("load config: %w", err)
			}

			// Use stored cluster URL if flag not provided.
			if clusterURL == "" {
				clusterURL = cfg.ClusterURL()
			}
			if clusterURL == "" {
				return fmt.Errorf("cluster URL is required; use --cluster <url>")
			}

			ts, err := auth.DeviceLogin(context.Background(), clusterURL)
			if err != nil {
				return fmt.Errorf("login: %w", err)
			}

			// Persist cluster URL to config.
			if err := cfg.SetClusterURL(clusterURL); err != nil {
				return fmt.Errorf("save cluster url: %w", err)
			}

			// Token stored in keychain — never print the raw value.
			w.Successf("Logged in to %s (token %s)", clusterURL, ts.MaskToken())
			return nil
		},
	}

	cmd.Flags().StringVar(&clusterURL, "cluster", "", "Gough cluster URL (e.g. https://gough.example.com)")
	return cmd
}

func newLogoutCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "logout",
		Short: "Remove stored credentials from the OS keychain",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()

			if err := auth.DeleteToken(); err != nil {
				return fmt.Errorf("delete token: %w", err)
			}
			w.Successf("Logged out; credentials removed from keychain")
			return nil
		},
	}
}
