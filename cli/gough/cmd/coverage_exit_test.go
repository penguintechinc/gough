//go:build noxdp

package cmd

import (
	"bytes"
	"testing"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

// mockExit replaces osExit for testing and captures the exit code.
// Uses t.Cleanup to ensure restoration even if test panics.
func mockExit(t *testing.T) *int {
	t.Helper()
	code := -1
	old := osExit
	osExit = func(c int) { code = c }
	t.Cleanup(func() { osExit = old })
	return &code
}

// TestClusterStatusCmd_ServiceUnavailable covers clusterStatusCmd with HTTP 503 (mapped to exit code 9).
// Tests that the special case handler for code 9 executes and calls osExit(9).
func TestClusterStatusCmd_ServiceUnavailable(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 503, map[string]interface{}{"error": "cluster unhealthy"}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()

	// 503 is mapped to exit code 9 by the client
	if *exitCode != 9 {
		t.Errorf("expected exit code 9, got %d", *exitCode)
	}
}

// TestHandleAPIError_ExitCode3 tests that handleAPIError calls osExit with code 3 (unauthorized).
func TestHandleAPIError_ExitCode3(t *testing.T) {
	code := mockExit(t)

	w := newWriter()
	apiErr := &client.APIError{Code: 3, Message: "unauthorized"}
	_ = handleAPIError(apiErr, w)

	if *code != 3 {
		t.Errorf("expected exit code 3, got %d", *code)
	}
}

// TestHandleAPIError_ExitCode4 tests tenant mismatch error.
func TestHandleAPIError_ExitCode4(t *testing.T) {
	code := mockExit(t)

	w := newWriter()
	apiErr := &client.APIError{Code: 4, Message: "tenant mismatch"}
	_ = handleAPIError(apiErr, w)

	if *code != 4 {
		t.Errorf("expected exit code 4, got %d", *code)
	}
}

// TestHandleAPIError_ExitCode5 tests rate limit error.
func TestHandleAPIError_ExitCode5(t *testing.T) {
	code := mockExit(t)

	w := newWriter()
	apiErr := &client.APIError{Code: 5, Message: "rate limited"}
	_ = handleAPIError(apiErr, w)

	if *code != 5 {
		t.Errorf("expected exit code 5, got %d", *code)
	}
}

// TestHandleAPIError_ExitCode6 tests validation error.
func TestHandleAPIError_ExitCode6(t *testing.T) {
	code := mockExit(t)

	w := newWriter()
	apiErr := &client.APIError{Code: 6, Message: "validation failed"}
	_ = handleAPIError(apiErr, w)

	if *code != 6 {
		t.Errorf("expected exit code 6, got %d", *code)
	}
}

// TestHandleAPIError_ExitCode7 tests plan compilation error.
func TestHandleAPIError_ExitCode7(t *testing.T) {
	code := mockExit(t)

	w := newWriter()
	apiErr := &client.APIError{Code: 7, Message: "plan compilation failed"}
	_ = handleAPIError(apiErr, w)

	if *code != 7 {
		t.Errorf("expected exit code 7, got %d", *code)
	}
}

// TestHandleAPIError_ExitCode8 tests safety envelope error.
func TestHandleAPIError_ExitCode8(t *testing.T) {
	code := mockExit(t)

	w := newWriter()
	apiErr := &client.APIError{Code: 8, Message: "safety envelope rejected"}
	_ = handleAPIError(apiErr, w)

	if *code != 8 {
		t.Errorf("expected exit code 8, got %d", *code)
	}
}

// TestHandleAPIError_ExitCode9 tests cluster unhealthy error.
func TestHandleAPIError_ExitCode9(t *testing.T) {
	code := mockExit(t)

	w := newWriter()
	apiErr := &client.APIError{Code: 9, Message: "cluster unhealthy"}
	_ = handleAPIError(apiErr, w)

	if *code != 9 {
		t.Errorf("expected exit code 9, got %d", *code)
	}
}

// TestHandleAPIError_ExitCode10 tests DR drill error.
func TestHandleAPIError_ExitCode10(t *testing.T) {
	code := mockExit(t)

	w := newWriter()
	apiErr := &client.APIError{Code: 10, Message: "DR drill failed"}
	_ = handleAPIError(apiErr, w)

	if *code != 10 {
		t.Errorf("expected exit code 10, got %d", *code)
	}
}

// TestHandleAPIError_ExitCode2 tests vault sealed error.
func TestHandleAPIError_ExitCode2(t *testing.T) {
	code := mockExit(t)

	w := newWriter()
	apiErr := &client.APIError{Code: 2, Message: "vault sealed"}
	_ = handleAPIError(apiErr, w)

	if *code != 2 {
		t.Errorf("expected exit code 2, got %d", *code)
	}
}

