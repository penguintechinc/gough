//go:build noxdp

// Package exec provides a testable interface around os/exec.Command so that
// probe functions can be exercised without running real system binaries.
package exec

import (
	"bytes"
	"context"
	"os/exec"
)

// Runner abstracts os/exec.Command for testing.
type Runner interface {
	// Run executes name with the given args and returns combined stdout+stderr
	// output and any error.
	Run(ctx context.Context, name string, args ...string) ([]byte, error)
}

// OSRunner is the production Runner that calls os/exec.Command.
type OSRunner struct{}

// Run implements Runner using the real OS exec.
func (OSRunner) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	return buf.Bytes(), err
}
