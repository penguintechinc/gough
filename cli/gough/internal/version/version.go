//go:build noxdp

// Package version holds build-time version metadata injected via -ldflags.
package version

import "fmt"

// Variables injected at build time via:
//
//	go build -ldflags="-X github.com/penguintechinc/gough/cli/gough/internal/version.Version=..."
var (
	Version   = "dev"
	Commit    = "unknown"
	BuildDate = "unknown"
	GoVersion = "unknown"
)

// String returns a single-line version string suitable for --version output.
func String() string {
	return fmt.Sprintf("%s (commit %s, built %s, %s)", Version, Commit, BuildDate, GoVersion)
}

// Info returns a structured map of version fields.
func Info() map[string]string {
	return map[string]string{
		"version":    Version,
		"commit":     Commit,
		"build_date": BuildDate,
		"go_version": GoVersion,
	}
}
