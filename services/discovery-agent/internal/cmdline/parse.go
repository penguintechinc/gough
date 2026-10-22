//go:build noxdp

// Package cmdline parses Gough-specific parameters from /proc/cmdline.
// The kernel passes these parameters during iPXE netboot to supply the
// bootstrap JWT, the api-manager endpoint, and the primary NIC MAC address.
package cmdline

import (
	"fmt"
	"os"
	"strings"
)

// Params holds values extracted from the kernel command line.
type Params struct {
	// Token is the one-time bootstrap JWT from gough_token=.
	Token string
	// Primary is the api-manager base URL from gough_primary= (e.g. "https://api.gough.example.com").
	Primary string
	// MAC is the primary NIC MAC address from gough_mac= (e.g. "aa:bb:cc:dd:ee:ff").
	MAC string
}

// Parse reads /proc/cmdline and extracts Gough-specific kernel parameters.
// It returns an error if any required parameter is missing.
func Parse() (Params, error) {
	raw, err := os.ReadFile("/proc/cmdline")
	if err != nil {
		return Params{}, fmt.Errorf("read /proc/cmdline: %w", err)
	}
	return ParseString(strings.TrimSpace(string(raw)))
}

// ParseString parses a raw cmdline string (the contents of /proc/cmdline
// without the trailing newline).  Exported for testing without filesystem access.
func ParseString(cmdline string) (Params, error) {
	var p Params
	for _, field := range strings.Fields(cmdline) {
		switch {
		case strings.HasPrefix(field, "gough_token="):
			p.Token = strings.TrimPrefix(field, "gough_token=")
		case strings.HasPrefix(field, "gough_primary="):
			p.Primary = strings.TrimPrefix(field, "gough_primary=")
		case strings.HasPrefix(field, "gough_mac="):
			p.MAC = strings.TrimPrefix(field, "gough_mac=")
		}
	}

	if p.Token == "" {
		return Params{}, fmt.Errorf("gough_token not found in cmdline")
	}
	if p.Primary == "" {
		return Params{}, fmt.Errorf("gough_primary not found in cmdline")
	}
	if p.MAC == "" {
		return Params{}, fmt.Errorf("gough_mac not found in cmdline")
	}

	return p, nil
}
