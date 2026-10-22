//go:build noxdp

package probe_test

import (
	"context"
	"os"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

func TestRunSmart_NVMe(t *testing.T) {
	fixture, err := os.ReadFile("testdata/smartctl_nvme.json")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}

	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"smartctl": {Output: fixture},
		},
	}

	result, err := probe.RunSmart(context.Background(), runner, "/dev/nvme0n1")
	if err != nil {
		t.Fatalf("RunSmart: %v", err)
	}

	if result.ModelName != "Samsung PM9A3 3.84TB" {
		t.Errorf("ModelName = %q, want Samsung PM9A3 3.84TB", result.ModelName)
	}
	if !result.SmartStatus.Passed {
		t.Error("expected SMART status passed")
	}
	if result.DriveType != "nvme" {
		t.Errorf("DriveType = %q, want nvme", result.DriveType)
	}
	if result.NvmeSmartHealthInformation == nil {
		t.Error("expected NvMe health info, got nil")
	}
}

func TestRunSmart_SAS(t *testing.T) {
	fixture, err := os.ReadFile("testdata/smartctl_sas.json")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}

	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"smartctl": {Output: fixture},
		},
	}

	result, err := probe.RunSmart(context.Background(), runner, "/dev/sda")
	if err != nil {
		t.Fatalf("RunSmart: %v", err)
	}

	if result.ModelName != "ST16000NM001G-2KK103" {
		t.Errorf("ModelName = %q, want ST16000NM001G-2KK103", result.ModelName)
	}
	if result.DriveType != "sas" {
		t.Errorf("DriveType = %q, want sas", result.DriveType)
	}
	if len(result.ATASmartAttributes.Table) == 0 {
		t.Error("expected ATA SMART attributes")
	}
}

func TestRunSmartAllDisks_PartialFailure(t *testing.T) {
	nvmeFixture, _ := os.ReadFile("testdata/smartctl_nvme.json")

	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			// nvme0n1 succeeds, sda fails
			"smartctl -a -j /dev/nvme0n1": {Output: nvmeFixture},
			"smartctl -a -j /dev/sda":     {Output: nil, Err: os.ErrNotExist},
		},
	}

	disks := []probe.BlockDevice{
		{Name: "nvme0n1", Type: "disk"},
		{Name: "sda", Type: "disk"},
	}

	results, errs := probe.RunSmartAllDisks(context.Background(), runner, disks)
	if len(results) != 1 {
		t.Errorf("results = %d, want 1 (partial)", len(results))
	}
	if len(errs) != 1 {
		t.Errorf("errs = %d, want 1", len(errs))
	}
}

func TestRunSmart_ErrorOnEmptyOutput(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"smartctl": {Output: nil, Err: os.ErrNotExist},
		},
	}
	_, err := probe.RunSmart(context.Background(), runner, "/dev/sda")
	if err == nil {
		t.Error("expected error on empty output + command failure")
	}
}
