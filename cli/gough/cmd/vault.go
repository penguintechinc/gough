//go:build noxdp

package cmd

import (
	"bufio"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newVaultCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "vault",
		Short: "Vault operations",
		Long:  "Manage Vault (unseal, status).",
	}

	cmd.AddCommand(vaultUnsealCmd())
	return cmd
}

func vaultUnsealCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "unseal",
		Short: "Provide a Shamir unseal share interactively",
		Long: `Submit a Shamir unseal share to the Vault instance on the primary.

The share is read interactively from stdin and NEVER stored, logged, or passed
as a CLI argument.  Repeat this command for each required share (default: 3-of-5).`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			fmt.Print("Enter Vault unseal share: ")
			reader := bufio.NewReader(os.Stdin)
			share, err := reader.ReadString('\n')
			if err != nil {
				return fmt.Errorf("read share: %w", err)
			}
			share = strings.TrimSpace(share)
			if share == "" {
				return fmt.Errorf("unseal share cannot be empty")
			}

			resp, err := c.VaultUnseal(ctx, share)
			if err != nil {
				if apiErr, ok := err.(*client.APIError); ok && apiErr.ExitCode() == 2 {
					w.Errorf("Vault sealed; provide remaining shares")
					osExit(2)
				}
				return handleAPIError(err, w)
			}

			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}

			w.Successf("Unseal share accepted")
			return nil
		},
	}
}
