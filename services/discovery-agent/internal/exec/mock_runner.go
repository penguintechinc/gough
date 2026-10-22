//go:build noxdp

package exec

import (
	"context"
	"fmt"
)

// MockRunner implements Runner for testing. Each registered command name maps
// to a fixed response (output bytes + error).
type MockRunner struct {
	// Responses maps "name args..." key to a canned response.
	Responses map[string]MockResponse
}

// MockResponse holds canned output for a single mock command.
type MockResponse struct {
	Output []byte
	Err    error
}

// Run returns the canned response for name, or an error if not registered.
func (m *MockRunner) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	key := name
	if len(args) > 0 {
		key = name + " " + joinArgs(args)
	}

	// Exact match first.
	if r, ok := m.Responses[key]; ok {
		return r.Output, r.Err
	}
	// Fallback: match by command name only.
	if r, ok := m.Responses[name]; ok {
		return r.Output, r.Err
	}
	return nil, fmt.Errorf("mock: no response for %q", key)
}

func joinArgs(args []string) string {
	result := ""
	for i, a := range args {
		if i > 0 {
			result += " "
		}
		result += a
	}
	return result
}
