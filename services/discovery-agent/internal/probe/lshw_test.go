//go:build noxdp

package probe_test

import (
	"context"
	"os"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

func TestRunLshw_ParsesGoldenFixture(t *testing.T) {
	fixture, err := os.ReadFile("testdata/lshw.json")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}

	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lshw": {Output: fixture},
		},
	}

	out, err := probe.RunLshw(context.Background(), runner)
	if err != nil {
		t.Fatalf("RunLshw: %v", err)
	}

	if out.Root.Class != "system" {
		t.Errorf("root class = %q, want system", out.Root.Class)
	}
	if out.Root.Product != "PowerEdge R640" {
		t.Errorf("product = %q, want PowerEdge R640", out.Root.Product)
	}
	if len(out.Root.Children) == 0 {
		t.Error("expected children, got none")
	}
}

func TestRunLshw_WalkFindsProcessor(t *testing.T) {
	fixture, err := os.ReadFile("testdata/lshw.json")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}

	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lshw": {Output: fixture},
		},
	}

	out, err := probe.RunLshw(context.Background(), runner)
	if err != nil {
		t.Fatalf("RunLshw: %v", err)
	}

	var found bool
	out.Walk(func(n *probe.LshwNode) {
		if n.Class == "processor" {
			found = true
		}
	})
	if !found {
		t.Error("Walk: did not find processor node")
	}
}

func TestRunLshw_ErrorOnBadJSON(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lshw": {Output: []byte("not json")},
		},
	}
	_, err := probe.RunLshw(context.Background(), runner)
	if err == nil {
		t.Error("expected error on bad JSON")
	}
}

func TestRunLshw_ErrorOnCommandFailure(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lshw": {Output: nil, Err: os.ErrNotExist},
		},
	}
	_, err := probe.RunLshw(context.Background(), runner)
	if err == nil {
		t.Error("expected error on command failure")
	}
}
