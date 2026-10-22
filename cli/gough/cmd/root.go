//go:build noxdp

// Package cmd contains all cobra commands for the gough CLI.
package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/auth"
	"github.com/penguintechinc/gough/cli/gough/internal/client"
	"github.com/penguintechinc/gough/cli/gough/internal/config"
	"github.com/penguintechinc/gough/cli/gough/internal/output"
	internalversion "github.com/penguintechinc/gough/cli/gough/internal/version"
)

// osExit is a variable for os.Exit to allow test mocking.
var osExit = os.Exit

// Global flags.
var (
	flagOutput     string
	flagQuiet      bool
	flagNoColor    bool
	flagCluster    string
	flagTenant     string
	flagToken      string
	flagContext    string
	flagVerbose    bool
	flagCAFile     string
	flagInsecure   bool
)

// rootCmd is the base command.
var rootCmd = &cobra.Command{
	Use:   "gough",
	Short: "Gough platform CLI",
	Long: `gough is the operator and developer CLI for the Gough LXD/k8s-on-Debian
provisioning platform.

Authentication uses OIDC device-code flow — run 'gough login' on first use.
Tokens are stored in the OS keychain; never as CLI arguments or plaintext files.`,
	SilenceUsage:  true,
	SilenceErrors: true,
	Version:       internalversion.String(),
}

// Execute is the entry point called from main.
func Execute() error {
	return rootCmd.Execute()
}

func init() {
	rootCmd.PersistentFlags().StringVarP(&flagOutput, "output", "o", "table",
		`Output format: table (default), json, yaml, jsonpath=<expr>`)
	rootCmd.PersistentFlags().BoolVarP(&flagQuiet, "quiet", "q", false,
		"Suppress all output except errors")
	rootCmd.PersistentFlags().BoolVar(&flagNoColor, "no-color", false,
		"Disable color output")
	rootCmd.PersistentFlags().StringVar(&flagCluster, "cluster", "",
		"Cluster URL (overrides config; e.g. https://gough.example.com)")
	rootCmd.PersistentFlags().StringVar(&flagTenant, "tenant", "",
		"Tenant ID (overrides config)")
	rootCmd.PersistentFlags().StringVar(&flagToken, "token", "",
		"Bearer token for machine use (skips keychain; prefer GOUGH_TOKEN env var)")
	rootCmd.PersistentFlags().StringVar(&flagContext, "context", "",
		"Named context from config (future: multi-cluster)")
	rootCmd.PersistentFlags().BoolVarP(&flagVerbose, "verbose", "v", false,
		"Enable verbose HTTP request/response logging to stderr")
	rootCmd.PersistentFlags().StringVar(&flagCAFile, "ca-file", "",
		"Path to a PEM CA certificate file for TLS verification")
	rootCmd.PersistentFlags().BoolVar(&flagInsecure, "insecure", false,
		"Disable TLS certificate verification (DANGER: do not use in production)")

	rootCmd.PersistentPreRunE = func(cmd *cobra.Command, args []string) error {
		// Skip version check for commands that don't need auth/connectivity
		skip := map[string]bool{"version": true, "login": true, "logout": true, "completion": true, "__complete": true}
		if skip[cmd.Name()] || skip[cmd.Root().Name()] {
			return nil
		}
		go checkVersionOnce(cmd.Context())
		return nil
	}

	// Register all sub-commands.
	rootCmd.AddCommand(
		newVersionCmd(),
		newLoginCmd(),
		newLogoutCmd(),
		newConfigCmd(),
		newInitCmd(),
		newSyncCmd(),
		newVaultCmd(),
		newPrimaryCmd(),
		newNodeCmd(),
		newDiskCmd(),
		newBiomeCmd(),
		newClusterCmd(),
		newLXDCmd(),
		newCapacityCmd(),
		newMigrationCmd(),
		newDRCmd(),
		newRestoreCmd(),
		newAuditCmd(),
		newDevCmd(),
		newIntegrationsCmd(),
		newDoctorCmd(),
		newWebhookCmd(),
		newDeploymentCmd(),
		newStorageCmd(),
		newCompletionCmd(),
	)
}

// newWriter creates an output.Writer from the global flags.
func newWriter() *output.Writer {
	return output.New(flagOutput, flagQuiet, flagNoColor)
}

// resolveClient loads config and token then returns a ready API client.
// Global --cluster, --tenant, --context, and --token flags override config values.
// If no token is found it exits with a helpful message.
func resolveClient(ctx context.Context) (*client.Client, *output.Writer, error) {
	w := newWriter()

	cfg, err := config.New()
	if err != nil {
		return nil, w, fmt.Errorf("load config: %w", err)
	}

	// --cluster flag overrides config.
	clusterURL := flagCluster
	if clusterURL == "" {
		clusterURL = cfg.ClusterURL()
	}

	// --context flag provides defaults for clusterURL and tenant if not already set.
	effectiveTenant := flagTenant
	if flagContext != "" {
		namedCtx, ok := cfg.GetContext(flagContext)
		if !ok {
			return nil, w, fmt.Errorf("context %q not found; run `gough config get-contexts`", flagContext)
		}
		if clusterURL == "" {
			clusterURL = namedCtx.ClusterURL
		}
		if effectiveTenant == "" {
			effectiveTenant = namedCtx.TenantID
		}
	}

	if clusterURL == "" {
		return nil, w, fmt.Errorf("no cluster URL configured; run `gough login --cluster <url>` first")
	}

	// --token flag bypasses keychain (machine use; prefer GOUGH_TOKEN env var).
	var token string
	if flagToken != "" {
		token = flagToken
	} else {
		ts, err := auth.LoadToken()
		if err != nil {
			return nil, w, err
		}
		// Proactively refresh if expiring within 5 minutes.
		if ts.NeedsRefresh() && ts.RefreshToken != "" {
			if refreshed, rerr := auth.RefreshToken(ctx, ts); rerr == nil {
				ts = refreshed
			}
			// Refresh failure is non-fatal — proceed with potentially-expiring token.
		}
		token = ts.AccessToken
	}

	// Build client with TLS and verbose options.
	var opts []client.ClientOption
	if flagVerbose {
		opts = append(opts, client.WithVerbose(true))
	}
	if flagCAFile != "" {
		opts = append(opts, client.WithTLSCAFile(flagCAFile))
	}
	if flagInsecure {
		_, _ = fmt.Fprintln(os.Stderr, "WARNING: TLS certificate verification disabled — do not use in production")
		opts = append(opts, client.WithInsecureSkipVerify(true))
	}

	c := client.New(clusterURL, token, opts...)

	// --tenant flag (or context-provided tenant) overrides the tenant from config.
	if effectiveTenant != "" {
		c.SetTenantID(effectiveTenant)
	} else if t := cfg.Get("tenant_id"); t != "" {
		c.SetTenantID(t)
	}

	return c, w, nil
}

// handleAPIError maps APIError codes to cobra exit codes.
func handleAPIError(err error, w *output.Writer) error {
	if apiErr, ok := err.(*client.APIError); ok {
		w.Errorf("%s", apiErr.Message)
		osExit(apiErr.ExitCode())
	}
	w.Errorf("%v", err)
	return err
}

// checkVersionOnce performs a background version check against the cluster.
// Prints a one-line notice to stderr if the CLI is outdated.
// Runs in a goroutine — never blocks the main command.
func checkVersionOnce(ctx context.Context) {
	cfg, err := config.New()
	if err != nil {
		return
	}
	clusterURL := cfg.ClusterURL()
	if clusterURL == "" {
		return
	}
	ts, err := auth.LoadToken()
	if err != nil {
		return
	}
	c := client.New(clusterURL, ts.AccessToken)
	// Use a short timeout so this never delays the user.
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	resp, err := c.GetClientVersion(ctx)
	if err != nil || resp == nil {
		return
	}
	// Extract latest version from response data.
	var versionInfo struct {
		LatestVersion string `json:"latest_version"`
	}
	if err := json.Unmarshal(resp.Data, &versionInfo); err != nil || versionInfo.LatestVersion == "" {
		return
	}
	current := internalversion.Version
	if current != "dev" && versionInfo.LatestVersion != current {
		fmt.Fprintf(os.Stderr, "\n  Notice: gough CLI %s is available (you have %s). Run `gough update` to upgrade.\n\n",
			versionInfo.LatestVersion, current)
	}
}
