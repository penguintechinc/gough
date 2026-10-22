//go:build noxdp

// Command gough is the operator/developer CLI for the Gough provisioning platform.
//
// Authentication: tokens are never passed as CLI arguments.  On first run
// `gough login` performs an OIDC device-code flow and stores the token in the
// OS keychain.  Subsequent commands read the token from the keychain or from
// the GOUGH_TOKEN environment variable.
//
// Exit codes: 0=success, 1=generic, 2=Vault sealed, 3=insufficient scope,
// 4=tenant mismatch, 5=rate limited, 6=validation failed,
// 7=plan compilation failed, 8=safety envelope rejected,
// 9=cluster unhealthy, 10=DR drill failed.
package main

import (
	"os"

	"github.com/penguintechinc/gough/cli/gough/cmd"
)

func main() {
	if err := cmd.Execute(); err != nil {
		os.Exit(1)
	}
}
