//go:build noxdp

package probe

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
)

// TestReadTrimmedAndInt64 covers the sysfs scalar readers: missing, blank, padded, and non-numeric.
func TestReadTrimmedAndInt64(t *testing.T) {
	dir := t.TempDir()
	write := func(name, content string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		return p
	}

	tests := []struct {
		name    string
		path    string
		wantStr string
		wantInt int64
	}{
		{name: "missing file", path: filepath.Join(dir, "absent"), wantStr: "", wantInt: -1},
		{name: "padded number", path: write("padded", "  42\n"), wantStr: "42", wantInt: 42},
		{name: "blank file", path: write("blank", "\n"), wantStr: "", wantInt: -1},
		{name: "non-numeric", path: write("word", "unknown\n"), wantStr: "unknown", wantInt: -1},
		{name: "mac address", path: write("addr", "aa:bb:cc:dd:ee:ff\n"), wantStr: "aa:bb:cc:dd:ee:ff", wantInt: -1},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := readTrimmed(tc.path); got != tc.wantStr {
				t.Errorf("readTrimmed = %q, want %q", got, tc.wantStr)
			}
			if got := readInt64(tc.path); got != tc.wantInt {
				t.Errorf("readInt64 = %d, want %d", got, tc.wantInt)
			}
		})
	}
}

// TestDetectFirmware checks the efi marker directory drives the UEFI/BIOS decision.
func TestDetectFirmware(t *testing.T) {
	orig := efiDir
	t.Cleanup(func() { efiDir = orig })

	tests := []struct {
		name  string
		setup func(t *testing.T) string
		want  FirmwareType
	}{
		{name: "efi dir present", setup: func(t *testing.T) string { return t.TempDir() }, want: FirmwareUEFI},
		{name: "efi dir absent", setup: func(t *testing.T) string { return filepath.Join(t.TempDir(), "nope") }, want: FirmwareBIOS},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			efiDir = tc.setup(t)
			if got := DetectFirmware(); got != tc.want {
				t.Errorf("DetectFirmware = %q, want %q", got, tc.want)
			}
		})
	}
}

// TestReadDMI covers a missing DMI tree (error) and a populated one with a missing field.
func TestReadDMI(t *testing.T) {
	orig := dmiBase
	t.Cleanup(func() { dmiBase = orig })

	populated := t.TempDir()
	fields := map[string]string{
		"product_uuid": "11111111-2222-3333-4444-555555555555\n",
		"sys_vendor":   "Dell Inc.\n",
		"product_name": "PowerEdge R640\n",
		// bios_version intentionally absent: must come back empty, not fail the probe.
	}
	for name, v := range fields {
		if err := os.WriteFile(filepath.Join(populated, name), []byte(v), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	tests := []struct {
		name    string
		dir     string
		wantErr bool
		want    DMIInfo
	}{
		{name: "dmi tree absent", dir: filepath.Join(t.TempDir(), "nope"), wantErr: true},
		{
			name: "partial fields read and trimmed",
			dir:  populated,
			want: DMIInfo{
				ProductUUID: "11111111-2222-3333-4444-555555555555",
				SysVendor:   "Dell Inc.",
				ProductName: "PowerEdge R640",
				BIOSVersion: "",
			},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			dmiBase = tc.dir
			got, err := ReadDMI()
			if (err != nil) != tc.wantErr {
				t.Fatalf("ReadDMI error = %v, wantErr %v", err, tc.wantErr)
			}
			if !tc.wantErr && got != tc.want {
				t.Errorf("ReadDMI = %+v, want %+v", got, tc.want)
			}
		})
	}
}

// TestRunNUMA exercises each tool's failure mode: lscpu is mandatory, numactl and lstopo are best-effort.
func TestRunNUMA(t *testing.T) {
	lscpu := []byte(`{"lscpu":[{"field":"Architecture:","data":"x86_64"}]}`)
	numactl := []byte("available: 2 nodes (0-1)\nnode 0 cpus: 0 1 2\nnode 0 size: 32153 MB\nnode 1 cpus: 3 4\nnode 1 size: 16000 MB\n")
	lstopo := []byte(`{"topology":true}`)

	tests := []struct {
		name       string
		resp       map[string]exec.MockResponse
		wantErr    bool
		wantNodes  int
		wantLstopo bool
	}{
		{
			name: "all tools succeed",
			resp: map[string]exec.MockResponse{
				"lscpu":              {Output: lscpu},
				"numactl":            {Output: numactl},
				"lstopo-no-graphics": {Output: lstopo},
			},
			wantNodes: 2, wantLstopo: true,
		},
		{
			name:    "lscpu failure is fatal",
			resp:    map[string]exec.MockResponse{},
			wantErr: true,
		},
		{
			name:    "lscpu invalid JSON is fatal",
			resp:    map[string]exec.MockResponse{"lscpu": {Output: []byte("not json")}},
			wantErr: true,
		},
		{
			name: "numactl and lstopo absent are tolerated",
			resp: map[string]exec.MockResponse{"lscpu": {Output: lscpu}},
		},
		{
			name: "empty lstopo output is not recorded",
			resp: map[string]exec.MockResponse{
				"lscpu":              {Output: lscpu},
				"lstopo-no-graphics": {Output: []byte{}},
			},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			res, err := RunNUMA(context.Background(), &exec.MockRunner{Responses: tc.resp})
			if (err != nil) != tc.wantErr {
				t.Fatalf("RunNUMA error = %v, wantErr %v", err, tc.wantErr)
			}
			if tc.wantErr {
				return
			}
			if len(res.Numactl.NodeCPUs) != tc.wantNodes {
				t.Errorf("NUMA nodes = %d, want %d", len(res.Numactl.NodeCPUs), tc.wantNodes)
			}
			if (res.Lstopo.Raw != nil) != tc.wantLstopo {
				t.Errorf("lstopo raw present = %v, want %v", res.Lstopo.Raw != nil, tc.wantLstopo)
			}
		})
	}
}

// TestRunSmart covers drive-type classification and the error/parse split.
func TestRunSmart(t *testing.T) {
	tests := []struct {
		name     string
		out      string
		runErr   error
		wantErr  bool
		wantType string
	}{
		{name: "nvme by protocol", out: `{"device":{"type":"nvme","protocol":"NVMe"}}`, wantType: "nvme"},
		{name: "sas by protocol", out: `{"device":{"type":"scsi","protocol":"SAS"}}`, wantType: "sas"},
		{name: "scsi type maps to sas", out: `{"device":{"type":"scsi","protocol":""}}`, wantType: "sas"},
		{name: "ata protocol maps to sata", out: `{"device":{"type":"sat","protocol":"ATA"}}`, wantType: "sata"},
		{name: "unrecognised is unknown", out: `{"device":{"type":"usb","protocol":"x"}}`, wantType: "unknown"},
		{name: "invalid JSON", out: `not json`, wantErr: true},
		{name: "failure with no output", out: ``, runErr: errors.New("exit 2"), wantErr: true},
		{name: "non-zero exit with output still parsed", out: `{"device":{"protocol":"NVMe"}}`, runErr: errors.New("exit 4"), wantType: "nvme"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r := &exec.MockRunner{Responses: map[string]exec.MockResponse{
				"smartctl": {Output: []byte(tc.out), Err: tc.runErr},
			}}
			got, err := RunSmart(context.Background(), r, "/dev/sda")
			if (err != nil) != tc.wantErr {
				t.Fatalf("RunSmart error = %v, wantErr %v", err, tc.wantErr)
			}
			if tc.wantErr {
				return
			}
			if got.DriveType != tc.wantType {
				t.Errorf("DriveType = %q, want %q", got.DriveType, tc.wantType)
			}
			if string(got.Raw) != tc.out {
				t.Errorf("Raw not preserved verbatim")
			}
		})
	}
}

// TestRunAccelerators covers each vendor tool: present, failing, and empty output.
func TestRunAccelerators(t *testing.T) {
	lspci := []byte("00:02.0 VGA compatible controller [0300]: Intel Corporation [8086:1234] (rev 01)\n")
	nvidia := []byte("<nvidia_smi_log><gpu><product_name>A100</product_name></gpu></nvidia_smi_log>")

	tests := []struct {
		name       string
		resp       map[string]exec.MockResponse
		wantPCI    int
		wantNvidia int
		wantROCm   bool
		wantXPU    bool
	}{
		{name: "no tools available", resp: map[string]exec.MockResponse{}},
		{
			name: "all tools report",
			resp: map[string]exec.MockResponse{
				"lspci":      {Output: lspci},
				"nvidia-smi": {Output: nvidia},
				"rocm-smi":   {Output: []byte("GPU[0] : card0\n")},
				"xpu-smi":    {Output: []byte("Device 0: Max 1550")},
			},
			wantPCI: 1, wantNvidia: 1, wantROCm: true, wantXPU: true,
		},
		{
			name: "empty vendor output is ignored",
			resp: map[string]exec.MockResponse{
				"nvidia-smi": {Output: []byte{}},
				"rocm-smi":   {Output: []byte{}},
				"xpu-smi":    {Output: []byte{}},
			},
		},
		{
			name: "vendor tool errors are ignored",
			resp: map[string]exec.MockResponse{
				"lspci":      {Err: errors.New("no pciutils")},
				"nvidia-smi": {Output: nvidia, Err: errors.New("driver not loaded")},
			},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			res := RunAccelerators(context.Background(), &exec.MockRunner{Responses: tc.resp})
			if len(res.PCIDevices) != tc.wantPCI {
				t.Errorf("PCI devices = %d, want %d", len(res.PCIDevices), tc.wantPCI)
			}
			if len(res.NvidiaSmi.GPUs) != tc.wantNvidia {
				t.Errorf("nvidia GPUs = %d, want %d", len(res.NvidiaSmi.GPUs), tc.wantNvidia)
			}
			if res.ROCmSmi.Available != tc.wantROCm {
				t.Errorf("ROCm available = %v, want %v", res.ROCmSmi.Available, tc.wantROCm)
			}
			if res.XPUSMI.Available != tc.wantXPU {
				t.Errorf("XPU available = %v, want %v", res.XPUSMI.Available, tc.wantXPU)
			}
		})
	}
}

// TestParseNvidiaSmiInvalidXML asserts malformed XML reports the tool as unavailable.
func TestParseNvidiaSmiInvalidXML(t *testing.T) {
	out := parseNvidiaSmi([]byte("<nvidia_smi_log><gpu>"))
	if out.Available {
		t.Error("malformed XML must not report nvidia-smi as available")
	}
}

// TestAcceleratorMarshalJSON checks availability flags and the GPU array survive serialisation.
func TestAcceleratorMarshalJSON(t *testing.T) {
	tests := []struct {
		name     string
		in       AcceleratorProbeResult
		wantNvid bool
		wantROC  bool
		wantXPU  bool
		wantGPUs bool
	}{
		{name: "empty result", in: AcceleratorProbeResult{}},
		{
			name: "all available",
			in: AcceleratorProbeResult{
				NvidiaSmi: NvidiaSmiOutput{Available: true, GPUs: make([]NvidiaSmiGPU, 1)},
				ROCmSmi:   ROCmSMIOutput{Available: true},
				XPUSMI:    XPUSMIOutput{Available: true},
			},
			wantNvid: true, wantROC: true, wantXPU: true, wantGPUs: true,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			data, err := json.Marshal(tc.in)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			var m map[string]json.RawMessage
			if err := json.Unmarshal(data, &m); err != nil {
				t.Fatal(err)
			}
			if _, ok := m["pci_devices"]; !ok {
				t.Errorf("pci_devices missing: %s", data)
			}
			check := func(key string, want bool) {
				var v bool
				if err := json.Unmarshal(m[key], &v); err != nil {
					t.Fatalf("%s: %v", key, err)
				}
				if v != want {
					t.Errorf("%s = %v, want %v", key, v, want)
				}
			}
			check("nvidia_available", tc.wantNvid)
			check("rocm_available", tc.wantROC)
			check("intel_xpu_available", tc.wantXPU)
			_, hasGPUs := m["nvidia_gpus"]
			if hasGPUs != tc.wantGPUs {
				t.Errorf("nvidia_gpus present = %v, want %v (%s)", hasGPUs, tc.wantGPUs, strings.TrimSpace(string(data)))
			}
		})
	}
}
