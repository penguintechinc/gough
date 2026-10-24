//go:build noxdp

package discover_test

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/discover"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

// update regenerates testdata/discover_request.golden.json: go test -tags noxdp -run Golden -update.
var update = flag.Bool("update", false, "regenerate golden discovery payload fixtures")

// goldenPath is the contract fixture the api-manager contract test also loads.
const goldenPath = "testdata/discover_request.golden.json"

// mustUnmarshal decodes a literal probe fixture or fails the test.
func mustUnmarshal[T any](t *testing.T, raw string) T {
	t.Helper()
	var v T
	if err := json.Unmarshal([]byte(raw), &v); err != nil {
		t.Fatalf("fixture unmarshal: %v", err)
	}
	return v
}

// goldenInput is the fixed probe output that produces the golden payload.
func goldenInput(t *testing.T) discover.Input {
	t.Helper()
	return discover.Input{
		MAC:      "aa:bb:cc:dd:ee:01",
		Hostname: "gough-node-01",
		DMI: probe.DMIInfo{
			ProductUUID: "4c4c4544-0042-3510-8048-b1c04f4d5a32",
			SysVendor:   "Dell Inc.",
			ProductName: "PowerEdge R640",
			BIOSVersion: "2.19.1",
		},
		Firmware: probe.FirmwareUEFI,
		Lshw:     []byte(`{"id":"system","class":"system","product":"PowerEdge R640"}`),
		Lsblk:    []byte(`{"blockdevices":[{"name":"sda","type":"disk","size":"893.8G"}]}`),
		NICs: []probe.NICInfo{
			{Name: "eno1", Address: "aa:bb:cc:dd:ee:01", SpeedMb: 10000, PCISlot: "0000:3b:00.0"},
			{Name: "eno2", Address: "AA:BB:CC:DD:EE:02", SpeedMb: -1},
		},
		SMART: []probe.SmartOutput{mustUnmarshal[probe.SmartOutput](t,
			`{"device":{"name":"sda"},"ata_smart_attributes":{"table":[`+
				`{"id":5,"name":"Reallocated_Sector_Ct","value":100,"worst":100,"thresh":10,"raw":{"value":0}},`+
				`{"id":9,"name":"Power_On_Hours","value":96,"worst":96,"thresh":0,"raw":{"value":12345}}]}}`)},
		NUMA: probe.NUMAProbeResult{Numactl: probe.NumactlOutput{
			Available: 2,
			NodeCPUs:  map[int][]int{0: {0, 1, 2, 3}, 1: {4, 5, 6, 7}},
			NodeMem:   map[int]int64{0: 65536, 1: 65536},
		}},
		Accelerators: probe.AcceleratorProbeResult{PCIDevices: []probe.PCIDevice{
			{Slot: "3b:00.0", Device: "GA102 [GeForce RTX 3090]", VendorID: "10de", BusType: probe.BusTypePCIe},
			{Slot: "00:02.0", Device: "Alder Lake-P GT2", VendorID: "8086", BusType: probe.BusTypeIntegrated},
			{Slot: "e1:00.0", Device: "vGPU", VendorID: "abcd", BusType: probe.BusTypeMediated},
		}},
		HardwareTags: []string{"cpu:vendor:intel", "disk:ssd"},
	}
}

// TestBuildRequest_Golden pins the wire payload. Drift fails; -update rewrites the fixture.
func TestBuildRequest_Golden(t *testing.T) {
	req, err := discover.BuildRequest(goldenInput(t))
	if err != nil {
		t.Fatalf("BuildRequest: %v", err)
	}
	req.BMCTLSStatus = "ok"
	req.BMCCertSHA256 = strings.Repeat("ab", 32)

	got, err := json.MarshalIndent(req, "", "  ")
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	got = append(got, '\n')

	if *update {
		if err := os.MkdirAll(filepath.Dir(goldenPath), 0o750); err != nil {
			t.Fatalf("mkdir: %v", err)
		}
		if err := os.WriteFile(goldenPath, got, 0o600); err != nil {
			t.Fatalf("write golden: %v", err)
		}
	}

	want, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatalf("read golden (run with -update to create): %v", err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("payload drifted from %s; rerun with -update if intended\n--- got\n%s", goldenPath, got)
	}
}

// TestBuildRequest_RejectsInvalid asserts each hub-required field fails loudly when missing or malformed.
func TestBuildRequest_RejectsInvalid(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*discover.Input)
	}{
		{"empty DMI UUID", func(in *discover.Input) { in.DMI.ProductUUID = "" }},
		{"empty MAC", func(in *discover.Input) { in.MAC = "" }},
		{"unknown firmware", func(in *discover.Input) { in.Firmware = "" }},
		{"lshw is a JSON array", func(in *discover.Input) { in.Lshw = []byte(`[{"id":"x"}]`) }},
		{"lsblk is not JSON", func(in *discover.Input) { in.Lsblk = []byte(`not-json`) }},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			in := goldenInput(t)
			tc.mutate(&in)
			if _, err := discover.BuildRequest(in); err == nil {
				t.Fatal("expected error")
			}
		})
	}
}

// TestBuildRequest_Mapping covers optional-field omission and list non-nil guarantees.
func TestBuildRequest_Mapping(t *testing.T) {
	in := discover.Input{
		MAC:      "aa:bb:cc:dd:ee:09",
		DMI:      probe.DMIInfo{ProductUUID: "uuid-9"},
		Firmware: probe.FirmwareBIOS,
	}
	req, err := discover.BuildRequest(in)
	if err != nil {
		t.Fatalf("BuildRequest: %v", err)
	}
	if req.NumaTopology != nil {
		t.Error("NumaTopology must be nil when numactl reports no nodes")
	}
	if req.LshwJSON != nil || req.LsblkJSON != nil {
		t.Error("absent lshw/lsblk must be omitted")
	}
	if req.SmartAttributes == nil || req.Nics == nil || req.Accelerators == nil || req.HardwareTags == nil {
		t.Error("list fields must be non-nil so the hub never receives null")
	}
}
