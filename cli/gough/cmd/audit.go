//go:build noxdp

package cmd

import (
	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newAuditCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "audit",
		Short: "Audit log operations",
	}

	cmd.AddCommand(
		auditListCmd(),
		auditVerifyCmd(),
		auditExportCmd(),
	)
	return cmd
}

func auditListCmd() *cobra.Command {
	var (
		since    string
		tenantID string
	)

	cmd := &cobra.Command{
		Use:   "list",
		Short: "List audit events",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			events, err := c.ListAuditEvents(ctx, since, tenantID)
			if err != nil {
				return handleAPIError(err, w)
			}

			if len(events) == 0 {
				w.Infof("No audit events found")
				return nil
			}

			rows := make([][]string, 0, len(events))
			for _, e := range events {
				rows = append(rows, []string{
					e.ID, e.Timestamp, e.Actor, e.Action, e.Resource, e.TenantID,
				})
			}
			w.PrintTable(
				[]string{"ID", "TIMESTAMP", "ACTOR", "ACTION", "RESOURCE", "TENANT"},
				rows,
			)
			return nil
		},
	}

	cmd.Flags().StringVar(&since, "since", "", "Filter events since ISO-8601 timestamp (e.g. 2026-01-01T00:00:00Z)")
	cmd.Flags().StringVar(&tenantID, "tenant", "", "Filter by tenant ID")
	return cmd
}

func auditVerifyCmd() *cobra.Command {
	var (
		since string
		to    string
		input string
	)

	cmd := &cobra.Command{
		Use:   "verify",
		Short: "Verify the audit hash chain for integrity",
		Long: `Verify the audit log hash chain across the specified time range.

Returns 0 if the chain is intact, 1 if any breaks are found.
Provide --input to verify a previously exported .jsonl file offline.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			params := map[string]string{}
			if input != "" {
				params["input"] = input
			}

			resp, err := c.VerifyAuditChain(ctx, since, to)
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

	cmd.Flags().StringVar(&since, "since", "", "Start of verification range (ISO-8601)")
	cmd.Flags().StringVar(&to, "to", "", "End of verification range (ISO-8601)")
	cmd.Flags().StringVar(&input, "input", "", "Path to exported .jsonl file for offline verification")
	return cmd
}

func auditExportCmd() *cobra.Command {
	var (
		since  string
		output string
	)

	cmd := &cobra.Command{
		Use:   "export",
		Short: "Export audit events to a .jsonl file",
		Long: `Export audit events to a newline-delimited JSON file.

The export includes the full hash chain metadata for offline verification
via 'gough audit verify --input <file>'.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.ExportAuditLog(ctx, since, output)
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Audit export initiated to %s", output)
			return nil
		},
	}

	cmd.Flags().StringVar(&since, "since", "", "Export events from this ISO-8601 timestamp")
	cmd.Flags().StringVar(&output, "output", "", "Output file path (.jsonl)")
	_ = cmd.MarkFlagRequired("output")
	return cmd
}
