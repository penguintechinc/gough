//go:build noxdp

package cmd

import (
	"github.com/spf13/cobra"

	internalversion "github.com/penguintechinc/gough/cli/gough/internal/version"
)

func newVersionCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Print version information",
		Long:  "Print the gough CLI version, git commit, build date and Go version.",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			w := newWriter()
			info := internalversion.Info()
			pairs := [][2]string{
				{"version", info["version"]},
				{"commit", info["commit"]},
				{"build_date", info["build_date"]},
				{"go_version", info["go_version"]},
			}
			w.PrintKV(pairs)
			return nil
		},
	}
}
