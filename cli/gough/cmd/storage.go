//go:build noxdp

package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func newStorageCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "storage",
		Short: "Manage storage quotas and requests",
		Long:  "View storage quotas and submit quota increase requests.",
	}

	cmd.AddCommand(
		storageQuotaListCmd(),
		storageQuotaRequestCmd(),
	)
	return cmd
}

func storageQuotaListCmd() *cobra.Command {
	var tenantID string

	cmd := &cobra.Command{
		Use:   "list-quotas",
		Short: "List storage quotas",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			quotas, err := c.ListStorageQuotas(ctx, tenantID)
			if err != nil {
				return handleAPIError(err, w)
			}

			if len(quotas) == 0 {
				w.Infof("No quotas found")
				return nil
			}

			rows := make([][]string, 0, len(quotas))
			for _, q := range quotas {
				rows = append(rows, []string{
					q.ID, q.TenantID, q.ResourceType,
					fmt.Sprintf("%.1f %s", q.LimitValue, q.Unit),
					fmt.Sprintf("%.1f %s", q.UsedValue, q.Unit),
					q.UpdatedAt,
				})
			}
			w.PrintTable(
				[]string{"ID", "TENANT", "RESOURCE", "LIMIT", "USED", "UPDATED"},
				rows,
			)
			return nil
		},
	}

	cmd.Flags().StringVar(&tenantID, "tenant-id", "", "Filter by tenant ID")
	return cmd
}

func storageQuotaRequestCmd() *cobra.Command {
	var (
		tenantID       string
		resourceType   string
		requestedValue float64
		unit           string
		justification  string
	)

	cmd := &cobra.Command{
		Use:   "quota-request",
		Short: "Submit a storage quota increase request",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			c, w, err := resolveClient(ctx)
			if err != nil {
				return err
			}

			if tenantID == "" || resourceType == "" || requestedValue == 0 || unit == "" {
				return fmt.Errorf("--tenant-id, --resource-type, --requested-value, and --unit are required")
			}

			_, err = c.RequestStorageQuota(ctx, client.StorageQuotaRequestParams{
				TenantID:       tenantID,
				ResourceType:   resourceType,
				RequestedValue: requestedValue,
				Unit:           unit,
				Justification:  justification,
			})
			if err != nil {
				return handleAPIError(err, w)
			}

			w.Successf("Quota request submitted for tenant %s: %.1f %s of %s", tenantID, requestedValue, unit, resourceType)
			return nil
		},
	}

	cmd.Flags().StringVar(&tenantID, "tenant-id", "", "Tenant ID")
	cmd.Flags().StringVar(&resourceType, "resource-type", "", "Resource type (e.g. block_storage, object_storage)")
	cmd.Flags().Float64Var(&requestedValue, "requested-value", 0, "Requested quota value")
	cmd.Flags().StringVar(&unit, "unit", "", "Unit (e.g. GiB, TiB)")
	cmd.Flags().StringVar(&justification, "justification", "", "Reason for the quota increase")
	return cmd
}
