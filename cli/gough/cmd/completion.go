//go:build noxdp

package cmd

import (
	"os"

	"github.com/spf13/cobra"
)

// newCompletionCmd returns the shell completion command.
func newCompletionCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "completion",
		Short: "Generate shell completion scripts",
		Long: `Generate shell completion scripts for gough.

To load completions:

Bash:
  $ source <(gough completion bash)
  # To load permanently:
  $ gough completion bash > /etc/bash_completion.d/gough

Zsh:
  # Enable completion if not already done:
  $ echo "autoload -U compinit; compinit" >> ~/.zshrc
  $ source <(gough completion zsh)
  # Or install permanently:
  $ gough completion zsh > "${fpath[1]}/_gough"

Fish:
  $ gough completion fish | source
  $ gough completion fish > ~/.config/fish/completions/gough.fish

PowerShell:
  PS> gough completion powershell | Out-String | Invoke-Expression
  # To load permanently, add the above line to your PowerShell profile.`,
	}

	cmd.AddCommand(
		completionBashCmd(),
		completionZshCmd(),
		completionFishCmd(),
		completionPowerShellCmd(),
	)
	return cmd
}

func completionBashCmd() *cobra.Command {
	return &cobra.Command{
		Use:                   "bash",
		Short:                 "Generate bash completion script",
		DisableFlagsInUseLine: true,
		Args:                  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return rootCmd.GenBashCompletion(os.Stdout)
		},
	}
}

func completionZshCmd() *cobra.Command {
	return &cobra.Command{
		Use:                   "zsh",
		Short:                 "Generate zsh completion script",
		DisableFlagsInUseLine: true,
		Args:                  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return rootCmd.GenZshCompletion(os.Stdout)
		},
	}
}

func completionFishCmd() *cobra.Command {
	return &cobra.Command{
		Use:                   "fish",
		Short:                 "Generate fish completion script",
		DisableFlagsInUseLine: true,
		Args:                  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return rootCmd.GenFishCompletion(os.Stdout, true)
		},
	}
}

func completionPowerShellCmd() *cobra.Command {
	return &cobra.Command{
		Use:                   "powershell",
		Short:                 "Generate PowerShell completion script",
		DisableFlagsInUseLine: true,
		Args:                  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return rootCmd.GenPowerShellCompletionWithDesc(os.Stdout)
		},
	}
}
