//go:build noxdp

package probe_test

import (
	"context"
	"os"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

func TestRunLsblk_ParsesGoldenFixture(t *testing.T) {
	fixture, err := os.ReadFile("testdata/lsblk.json")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}

	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lsblk": {Output: fixture},
		},
	}

	out, err := probe.RunLsblk(context.Background(), runner)
	if err != nil {
		t.Fatalf("RunLsblk: %v", err)
	}

	if len(out.BlockDevices) != 2 {
		t.Errorf("BlockDevices = %d, want 2", len(out.BlockDevices))
	}
}

func TestRunLsblk_TopLevelDisks(t *testing.T) {
	fixture, err := os.ReadFile("testdata/lsblk.json")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}

	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lsblk": {Output: fixture},
		},
	}

	out, err := probe.RunLsblk(context.Background(), runner)
	if err != nil {
		t.Fatalf("RunLsblk: %v", err)
	}

	disks := out.TopLevelDisks()
	if len(disks) != 2 {
		t.Errorf("TopLevelDisks = %d, want 2", len(disks))
	}

	for _, d := range disks {
		if d.Type != "disk" {
			t.Errorf("disk type = %q, want disk", d.Type)
		}
	}
}

func TestRunLsblk_NVMeTransport(t *testing.T) {
	fixture, err := os.ReadFile("testdata/lsblk.json")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}

	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lsblk": {Output: fixture},
		},
	}

	out, err := probe.RunLsblk(context.Background(), runner)
	if err != nil {
		t.Fatalf("RunLsblk: %v", err)
	}

	disks := out.TopLevelDisks()
	nvmeFound := false
	for _, d := range disks {
		if d.Tran == "nvme" {
			nvmeFound = true
		}
	}
	if !nvmeFound {
		t.Error("expected nvme disk in fixture")
	}
}

func TestRunLsblk_ErrorOnBadJSON(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lsblk": {Output: []byte("{bad}")},
		},
	}
	_, err := probe.RunLsblk(context.Background(), runner)
	if err == nil {
		t.Error("expected error on bad JSON")
	}
}
