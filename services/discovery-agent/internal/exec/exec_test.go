//go:build noxdp

package exec_test

import (
	"context"
	"errors"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
)

func TestMockRunner_ExactMatch(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"echo hello": {Output: []byte("hello\n")},
		},
	}

	out, err := runner.Run(context.Background(), "echo", "hello")
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if string(out) != "hello\n" {
		t.Errorf("output = %q, want hello\\n", out)
	}
}

func TestMockRunner_NameOnlyFallback(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lshw": {Output: []byte(`{"id":"test"}`)},
		},
	}

	out, err := runner.Run(context.Background(), "lshw", "-json", "-quiet")
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if string(out) != `{"id":"test"}` {
		t.Errorf("output = %q", out)
	}
}

func TestMockRunner_ErrorResponse(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lshw": {Output: nil, Err: errors.New("command not found")},
		},
	}

	_, err := runner.Run(context.Background(), "lshw")
	if err == nil {
		t.Error("expected error")
	}
}

func TestMockRunner_UnregisteredCommand(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{},
	}

	_, err := runner.Run(context.Background(), "unknown-tool")
	if err == nil {
		t.Error("expected error for unregistered command")
	}
}

func TestOSRunner_Interface(t *testing.T) {
	// Verify OSRunner implements Runner.
	var _ exec.Runner = exec.OSRunner{}
}

func TestOSRunner_EchoCommand(t *testing.T) {
	runner := exec.OSRunner{}
	out, err := runner.Run(context.Background(), "echo", "gough-test")
	if err != nil {
		t.Fatalf("echo: %v", err)
	}
	if string(out) != "gough-test\n" {
		t.Errorf("echo output = %q", out)
	}
}
