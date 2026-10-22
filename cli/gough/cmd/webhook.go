//go:build noxdp

package cmd

import (
	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newWebhookCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "webhook",
		Short: "Webhook management",
	}

	cmd.AddCommand(webhookTestCmd())
	return cmd
}

func webhookTestCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "test <webhook-id>",
		Short: "Send a test event to a configured webhook",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			resp, err := c.TestWebhook(ctx, args[0])
			if err != nil {
				return handleAPIError(err, w)
			}
			if client.IsDeferred(resp) {
				w.DeferredNote(resp.Note)
				return nil
			}
			w.Successf("Test event sent to webhook %s", args[0])
			return nil
		},
	}
}
