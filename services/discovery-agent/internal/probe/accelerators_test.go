//go:build noxdp

package probe_test

import (
	"context"
	"os"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

const lspciGolden = `00:00.0 Host bridge [0600]: Intel Corporation Device [8086:3cb0] (rev 04)
00:02.0 VGA compatible controller [0300]: Intel Corporation UHD Graphics 730 [8086:4692] (rev 0c)
01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA102GL [RTX A5000] [10de:2231] (rev a1)
02:00.0 Infiniband controller [0207]: Mellanox Technologies MT28908 Family [ConnectX-7] [15b3:101b] (rev 20)
`

const nvidiaSmiGolden = `<?xml version="1.0" ?>
<nvidia_smi_log>
  <gpu id="00000000:01:00.0">
    <product_name>NVIDIA RTX A5000</product_name>
    <uuid>GPU-12345678-1234-1234-1234-1234567890ab</uuid>
    <pci><pci_bus_id>00000000:01:00.0</pci_bus_id></pci>
    <cuda_version>12.4</cuda_version>
    <driver_version>545.23.08</driver_version>
    <mig_mode><current_mig>Disabled</current_mig></mig_mode>
    <compute_cap_major>8</compute_cap_major>
    <compute_cap_minor>6</compute_cap_minor>
  </gpu>
</nvidia_smi_log>
`

func TestRunAccelerators_ParsesLspci(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lspci":    {Output: []byte(lspciGolden)},
			"nvidia-smi": {Output: []byte(nvidiaSmiGolden)},
			"rocm-smi": {Err: os.ErrNotExist},
			"xpu-smi":  {Err: os.ErrNotExist},
		},
	}

	result := probe.RunAccelerators(context.Background(), runner)

	if len(result.PCIDevices) == 0 {
		t.Fatal("expected PCI devices, got none")
	}

	// Find NVIDIA device.
	var nvidiaFound bool
	for _, d := range result.PCIDevices {
		if d.VendorID == "10de" {
			nvidiaFound = true
			if d.BusType != probe.BusTypePCIe {
				t.Errorf("NVIDIA bus type = %q, want pcie", d.BusType)
			}
		}
	}
	if !nvidiaFound {
		t.Error("NVIDIA device not found in PCI list")
	}
}

func TestRunAccelerators_ParsesNvidiaSmi(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lspci":      {Output: []byte(lspciGolden)},
			"nvidia-smi": {Output: []byte(nvidiaSmiGolden)},
			"rocm-smi":   {Err: os.ErrNotExist},
			"xpu-smi":    {Err: os.ErrNotExist},
		},
	}

	result := probe.RunAccelerators(context.Background(), runner)

	if !result.NvidiaSmi.Available {
		t.Fatal("expected nvidia-smi available")
	}
	if len(result.NvidiaSmi.GPUs) != 1 {
		t.Errorf("GPUs = %d, want 1", len(result.NvidiaSmi.GPUs))
	}
	g := result.NvidiaSmi.GPUs[0]
	if g.ComputeCapMajor != "8" {
		t.Errorf("compute cap major = %q, want 8", g.ComputeCapMajor)
	}
	if g.ComputeCapMinor != "6" {
		t.Errorf("compute cap minor = %q, want 6", g.ComputeCapMinor)
	}
}

func TestRunAccelerators_IntegratedGPU(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lspci":      {Output: []byte(lspciGolden)},
			"nvidia-smi": {Err: os.ErrNotExist},
			"rocm-smi":   {Err: os.ErrNotExist},
			"xpu-smi":    {Err: os.ErrNotExist},
		},
	}

	result := probe.RunAccelerators(context.Background(), runner)

	var intelIGPUFound bool
	for _, d := range result.PCIDevices {
		if d.VendorID == "8086" && d.BusType == probe.BusTypeIntegrated {
			intelIGPUFound = true
		}
	}
	if !intelIGPUFound {
		t.Error("Intel integrated GPU not found")
	}
}

func TestRunAccelerators_NoAccelerators(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lspci":      {Output: []byte("00:00.0 Host bridge [0600]: Intel Corporation [8086:3cb0]\n")},
			"nvidia-smi": {Err: os.ErrNotExist},
			"rocm-smi":   {Err: os.ErrNotExist},
			"xpu-smi":    {Err: os.ErrNotExist},
		},
	}

	result := probe.RunAccelerators(context.Background(), runner)
	if result.NvidiaSmi.Available {
		t.Error("nvidia-smi should not be available")
	}
}
