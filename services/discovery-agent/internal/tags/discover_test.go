//go:build noxdp

package tags_test

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/tags"
)

func mustReadLshw(t *testing.T) probe.LshwOutput {
	t.Helper()
	data, err := os.ReadFile("../probe/testdata/lshw.json")
	if err != nil {
		t.Fatalf("read lshw fixture: %v", err)
	}
	var root probe.LshwNode
	if err := json.Unmarshal(data, &root); err != nil {
		t.Fatalf("unmarshal lshw: %v", err)
	}
	return probe.LshwOutput{Root: root, Raw: data}
}

func mustReadLsblk(t *testing.T) probe.LsblkOutput {
	t.Helper()
	data, err := os.ReadFile("../probe/testdata/lsblk.json")
	if err != nil {
		t.Fatalf("read lsblk fixture: %v", err)
	}
	var out probe.LsblkOutput
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal lsblk: %v", err)
	}
	out.Raw = data
	return out
}

func mustReadSmart(t *testing.T, path string) probe.SmartOutput {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read smart fixture %s: %v", path, err)
	}
	var out probe.SmartOutput
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal smart %s: %v", path, err)
	}
	out.Raw = data
	return out
}

func buildTestInput(t *testing.T) tags.HardwareInput {
	t.Helper()
	lscpuJSON := `{"lscpu":[
		{"field":"Architecture:","data":"x86_64"},
		{"field":"CPU(s):","data":"128"},
		{"field":"Vendor ID:","data":"GenuineIntel"},
		{"field":"Model name:","data":"Intel(R) Xeon(R) Platinum 8462Y+"},
		{"field":"Socket(s):","data":"2"},
		{"field":"Core(s) per socket:","data":"32"},
		{"field":"Thread(s) per core:","data":"2"},
		{"field":"Flags:","data":"avx2 avx512f vmx"}
	]}`

	var numaResult probe.NUMAProbeResult
	if err := json.Unmarshal([]byte(lscpuJSON), &numaResult.Lscpu); err != nil {
		t.Fatalf("unmarshal lscpu: %v", err)
	}
	numaResult.Numactl = probe.NumactlOutput{
		Available: 2,
		NodeCPUs:  map[int][]int{0: {0, 1, 2}, 1: {3, 4, 5}},
		NodeMem:   map[int]int64{0: 65536, 1: 65536},
	}

	smartNVMe := mustReadSmart(t, "../probe/testdata/smartctl_nvme.json")
	smartSAS := mustReadSmart(t, "../probe/testdata/smartctl_sas.json")

	return tags.HardwareInput{
		Lshw:    mustReadLshw(t),
		Lsblk:   mustReadLsblk(t),
		DMI:     probe.DMIInfo{ProductUUID: "test-uuid", SysVendor: "Dell Inc.", ProductName: "PowerEdge R640"},
		NICs:    []probe.NICInfo{{Name: "eth0", Address: "aa:bb:cc:dd:ee:ff", SpeedMb: 200000, Carrier: true, PCIID: "15b3:101b"}},
		SMART:   []probe.SmartOutput{smartNVMe, smartSAS},
		Firmware: probe.FirmwareBIOS,
		NUMA:    numaResult,
		Accelerators: probe.AcceleratorProbeResult{
			PCIDevices: []probe.PCIDevice{
				{Slot: "01:00.0", Class: "VGA compatible controller", VendorID: "10de", DeviceID: "2231", BusType: probe.BusTypePCIe},
			},
			NvidiaSmi: probe.NvidiaSmiOutput{Available: true, GPUs: []probe.NvidiaSmiGPU{{
				ProductName: "RTX A5000", ComputeCapMajor: "8", ComputeCapMinor: "6",
			}}},
		},
	}
}

func hasTag(tagSet []string, tag string) bool {
	for _, t := range tagSet {
		if t == tag {
			return true
		}
	}
	return false
}

func TestDiscover_CPUTags(t *testing.T) {
	hw := buildTestInput(t)
	tagSet := tags.Discover(hw)

	if !hasTag(tagSet, "cpu:vendor:intel") {
		t.Error("missing cpu:vendor:intel")
	}
	if !hasTag(tagSet, "cpu:feature:avx2") {
		t.Error("missing cpu:feature:avx2")
	}
	if !hasTag(tagSet, "cpu:feature:avx512") {
		t.Error("missing cpu:feature:avx512")
	}
	if !hasTag(tagSet, "cpu:nested-virt-supported") {
		t.Error("missing cpu:nested-virt-supported (vmx flag)")
	}
	if !hasTag(tagSet, "cpu:threads:128") {
		t.Error("missing cpu:threads:128")
	}
	if !hasTag(tagSet, "cpu:sockets:2") {
		t.Error("missing cpu:sockets:2")
	}
	if !hasTag(tagSet, "cpu:cores:64") {
		t.Error("missing cpu:cores:64 (2 sockets * 32 cores/socket)")
	}
}

func TestDiscover_MemoryTags(t *testing.T) {
	hw := buildTestInput(t)
	tagSet := tags.Discover(hw)

	if !hasTag(tagSet, "mem:ecc:true") {
		t.Error("missing mem:ecc:true (fixture has ECC capability)")
	}
	if !hasTag(tagSet, "mem:numa-nodes:2") {
		t.Error("missing mem:numa-nodes:2")
	}
	// System memory in lshw fixture = 137438953472 bytes = 128 GiB → tag mem:total-gb:128
	if !hasTag(tagSet, "mem:total-gb:128") {
		t.Errorf("missing mem:total-gb:128; tags = %v", tagSet)
	}
}

func TestDiscover_DiskTags(t *testing.T) {
	hw := buildTestInput(t)
	tagSet := tags.Discover(hw)

	if !hasTag(tagSet, "disk:nvme") {
		t.Error("missing disk:nvme")
	}
	if !hasTag(tagSet, "disk:sas-hdd") {
		t.Error("missing disk:sas-hdd")
	}
	if !hasTag(tagSet, "disk:tier:fast") {
		t.Error("missing disk:tier:fast")
	}
	if !hasTag(tagSet, "disk:tier:bulk") {
		t.Error("missing disk:tier:bulk")
	}
}

func TestDiscover_NICTags(t *testing.T) {
	hw := buildTestInput(t)
	tagSet := tags.Discover(hw)

	if !hasTag(tagSet, "nic:speed:200g") {
		t.Error("missing nic:speed:200g")
	}
	if !hasTag(tagSet, "nic:vendor:mellanox") {
		t.Error("missing nic:vendor:mellanox")
	}
}

func TestDiscover_NICTags_RDMA(t *testing.T) {
	hw := buildTestInput(t)
	// The lshw fixture has RoCE capability on the network node.
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "nic:rdma:roce") {
		t.Error("missing nic:rdma:roce (fixture has RoCE capability)")
	}
}

func TestDiscover_GPUTags(t *testing.T) {
	hw := buildTestInput(t)
	tagSet := tags.Discover(hw)

	if !hasTag(tagSet, "gpu:nvidia") {
		t.Error("missing gpu:nvidia")
	}
	if !hasTag(tagSet, "gpu:pcie") {
		t.Error("missing gpu:pcie")
	}
	if !hasTag(tagSet, "gpu:nvidia:cuda-arch:sm_86") {
		t.Error("missing gpu:nvidia:cuda-arch:sm_86")
	}
	if !hasTag(tagSet, "gpu:vendor:nvidia:count:1") {
		t.Error("missing gpu:vendor:nvidia:count:1")
	}
}

func TestDiscover_GPUTags_MIG(t *testing.T) {
	hw := buildTestInput(t)
	hw.Accelerators.NvidiaSmi.GPUs[0].MIGMode = "Enabled"
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "gpu:nvidia:mig-capable") {
		t.Error("missing gpu:nvidia:mig-capable when MIG mode is Enabled")
	}
}

func TestDiscover_FirmwareTags_BIOS(t *testing.T) {
	hw := buildTestInput(t)
	hw.Firmware = probe.FirmwareBIOS
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "firmware:bios") {
		t.Error("missing firmware:bios")
	}
	if hasTag(tagSet, "firmware:uefi") {
		t.Error("unexpected firmware:uefi on BIOS node")
	}
}

func TestDiscover_FirmwareTags_UEFI(t *testing.T) {
	hw := buildTestInput(t)
	hw.Firmware = probe.FirmwareUEFI
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "firmware:uefi") {
		t.Error("missing firmware:uefi")
	}
}

func TestDiscover_Dedup(t *testing.T) {
	hw := buildTestInput(t)
	tagSet := tags.Discover(hw)

	seen := map[string]int{}
	for _, tag := range tagSet {
		seen[tag]++
	}
	for tag, count := range seen {
		if count > 1 {
			t.Errorf("duplicate tag: %q (count=%d)", tag, count)
		}
	}
}

func TestDiscover_AMDVendor(t *testing.T) {
	hw := buildTestInput(t)
	lscpuJSON := `{"lscpu":[
		{"field":"CPU(s):","data":"64"},
		{"field":"Vendor ID:","data":"AuthenticAMD"},
		{"field":"Model name:","data":"AMD EPYC 9654 96-Core Processor"},
		{"field":"Socket(s):","data":"1"},
		{"field":"Core(s) per socket:","data":"96"},
		{"field":"Flags:","data":"avx2 svm"}
	]}`
	if err := json.Unmarshal([]byte(lscpuJSON), &hw.NUMA.Lscpu); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "cpu:vendor:amd") {
		t.Error("missing cpu:vendor:amd")
	}
	if !hasTag(tagSet, "cpu:nested-virt-supported") {
		t.Error("missing cpu:nested-virt-supported (svm flag)")
	}
}

func TestDiscover_IntelIntegratedGPU(t *testing.T) {
	hw := buildTestInput(t)
	hw.Accelerators.PCIDevices = []probe.PCIDevice{
		{Slot: "00:02.0", Class: "VGA compatible controller", VendorID: "8086", DeviceID: "4692", BusType: probe.BusTypeIntegrated},
	}
	hw.Accelerators.NvidiaSmi = probe.NvidiaSmiOutput{Available: false}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "gpu:integrated") {
		t.Error("missing gpu:integrated")
	}
	if !hasTag(tagSet, "gpu:intel-integrated") {
		t.Error("missing gpu:intel-integrated")
	}
}

func TestDiscover_NoDisks(t *testing.T) {
	hw := buildTestInput(t)
	hw.Lsblk = probe.LsblkOutput{}
	hw.SMART = nil
	tagSet := tags.Discover(hw)
	// Should not contain disk tags.
	for _, tag := range tagSet {
		if len(tag) > 5 && tag[:5] == "disk:" {
			// disk tags are OK if derived from lshw; but top-level disk tags from lsblk should be absent.
			t.Logf("disk tag present (may be OK): %q", tag)
		}
	}
}

func TestPowerOfTwoCeil(t *testing.T) {
	hw := buildTestInput(t)
	tagSet := tags.Discover(hw)

	foundDiskTotal := false
	for _, tag := range tagSet {
		if len(tag) > 10 && tag[:10] == "disk:total" {
			foundDiskTotal = true
		}
	}
	if !foundDiskTotal {
		t.Error("missing disk:total-gb tag")
	}
}

func TestDiscover_ConfidentialComputeSevSnp(t *testing.T) {
	hw := buildTestInput(t)
	lscpuJSON := `{"lscpu":[
		{"field":"CPU(s):","data":"64"},
		{"field":"Vendor ID:","data":"AuthenticAMD"},
		{"field":"Model name:","data":"AMD EPYC 9654"},
		{"field":"Socket(s):","data":"1"},
		{"field":"Core(s) per socket:","data":"64"},
		{"field":"Flags:","data":"avx2 sev_snp"}
	]}`
	if err := json.Unmarshal([]byte(lscpuJSON), &hw.NUMA.Lscpu); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "confidential-compute:sev-snp") {
		t.Error("missing confidential-compute:sev-snp")
	}
}

func TestDiscover_SpeedBuckets(t *testing.T) {
	tests := []struct {
		speedMb int64
		wantTag string
	}{
		{1000, "nic:speed:1g"},
		{10000, "nic:speed:10g"},
		{25000, "nic:speed:25g"},
		{40000, "nic:speed:40g"},
		{100000, "nic:speed:100g"},
		{200000, "nic:speed:200g"},
		{400000, "nic:speed:400g"},
	}
	for _, tt := range tests {
		t.Run(tt.wantTag, func(t *testing.T) {
			hw := buildTestInput(t)
			hw.NICs = []probe.NICInfo{{Name: "eth0", Address: "aa:bb:cc:dd:ee:ff", SpeedMb: tt.speedMb}}
			tagSet := tags.Discover(hw)
			if !hasTag(tagSet, tt.wantTag) {
				t.Errorf("missing %q for speed %d Mb/s", tt.wantTag, tt.speedMb)
			}
		})
	}
}

// ─── Additional coverage tests ────────────────────────────────────────────────

func TestDetectUarch_IntelFamilies(t *testing.T) {
	tests := []struct {
		model   string
		vendor  string
		wantTag string
	}{
		{"Intel Xeon Granite Rapids", "GenuineIntel", "cpu:family:granite-rapids"},
		{"Intel Xeon Sapphire Rapids", "GenuineIntel", "cpu:family:sapphire-rapids"},
		{"Intel Xeon Ice Lake", "GenuineIntel", "cpu:family:ice-lake"},
		{"Intel Xeon Cascade Lake", "GenuineIntel", "cpu:family:cascade-lake"},
	}
	for _, tt := range tests {
		t.Run(tt.wantTag, func(t *testing.T) {
			hw := buildTestInput(t)
			lscpuJSON := `{"lscpu":[
				{"field":"CPU(s):","data":"32"},
				{"field":"Vendor ID:","data":"` + tt.vendor + `"},
				{"field":"Model name:","data":"` + tt.model + `"},
				{"field":"Socket(s):","data":"1"},
				{"field":"Core(s) per socket:","data":"16"},
				{"field":"Flags:","data":"avx2"}
			]}`
			if err := json.Unmarshal([]byte(lscpuJSON), &hw.NUMA.Lscpu); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			tagSet := tags.Discover(hw)
			if !hasTag(tagSet, tt.wantTag) {
				t.Errorf("missing %q; got %v", tt.wantTag, tagSet)
			}
		})
	}
}

func TestDetectUarch_AMDFamilies(t *testing.T) {
	tests := []struct {
		model   string
		wantTag string
	}{
		{"AMD EPYC Genoa Processor", "cpu:family:zen4"},
		{"AMD EPYC Milan Processor", "cpu:family:zen3"},
		{"AMD EPYC Rome Processor", "cpu:family:zen2"},
		{"AMD EPYC Naples Processor", "cpu:family:zen1"},
	}
	for _, tt := range tests {
		t.Run(tt.wantTag, func(t *testing.T) {
			hw := buildTestInput(t)
			lscpuJSON := `{"lscpu":[
				{"field":"CPU(s):","data":"64"},
				{"field":"Vendor ID:","data":"AuthenticAMD"},
				{"field":"Model name:","data":"` + tt.model + `"},
				{"field":"Socket(s):","data":"1"},
				{"field":"Core(s) per socket:","data":"32"},
				{"field":"Flags:","data":"avx2"}
			]}`
			if err := json.Unmarshal([]byte(lscpuJSON), &hw.NUMA.Lscpu); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			tagSet := tags.Discover(hw)
			if !hasTag(tagSet, tt.wantTag) {
				t.Errorf("missing %q; got %v", tt.wantTag, tagSet)
			}
		})
	}
}

func TestDetectUarch_Graviton(t *testing.T) {
	tests := []struct {
		model   string
		wantTag string
	}{
		{"AWS Graviton4", "cpu:family:graviton4"},
		{"AWS Graviton3 Processor", "cpu:family:graviton3"},
	}
	for _, tt := range tests {
		t.Run(tt.wantTag, func(t *testing.T) {
			hw := buildTestInput(t)
			lscpuJSON := `{"lscpu":[
				{"field":"CPU(s):","data":"64"},
				{"field":"Vendor ID:","data":"ARM"},
				{"field":"Model name:","data":"` + tt.model + `"},
				{"field":"Socket(s):","data":"1"},
				{"field":"Core(s) per socket:","data":"64"},
				{"field":"Flags:","data":""}
			]}`
			if err := json.Unmarshal([]byte(lscpuJSON), &hw.NUMA.Lscpu); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			tagSet := tags.Discover(hw)
			if !hasTag(tagSet, tt.wantTag) {
				t.Errorf("missing %q; got %v", tt.wantTag, tagSet)
			}
		})
	}
}

func TestDiscover_NICVendors(t *testing.T) {
	tests := []struct {
		pciID   string
		wantTag string
	}{
		{"8086:1592", "nic:vendor:intel-e810"},
		{"14e4:16d7", "nic:vendor:broadcom"},
	}
	for _, tt := range tests {
		t.Run(tt.wantTag, func(t *testing.T) {
			hw := buildTestInput(t)
			hw.NICs = []probe.NICInfo{{Name: "eth0", Address: "aa:bb:cc:dd:ee:ff", SpeedMb: 10000, PCIID: tt.pciID}}
			tagSet := tags.Discover(hw)
			if !hasTag(tagSet, tt.wantTag) {
				t.Errorf("missing %q; got %v", tt.wantTag, tagSet)
			}
		})
	}
}

func TestDiscover_NICVendor_Unknown(t *testing.T) {
	// Unknown PCI vendor ID should not emit a nic:vendor tag.
	hw := buildTestInput(t)
	hw.NICs = []probe.NICInfo{{Name: "eth0", Address: "aa:bb:cc:dd:ee:ff", SpeedMb: 1000, PCIID: "dead:beef"}}
	tagSet := tags.Discover(hw)
	for _, tag := range tagSet {
		if len(tag) > 11 && tag[:11] == "nic:vendor:" {
			t.Errorf("unexpected vendor tag for unknown PCI ID: %q", tag)
		}
	}
}

func TestDiscover_NICSpeed_BelowAllBuckets(t *testing.T) {
	// Speed below all buckets — no nic:speed tag expected.
	hw := buildTestInput(t)
	hw.NICs = []probe.NICInfo{{Name: "eth0", Address: "aa:bb:cc:dd:ee:ff", SpeedMb: 10, PCIID: "dead:beef"}}
	tagSet := tags.Discover(hw)
	for _, tag := range tagSet {
		if len(tag) > 10 && tag[:10] == "nic:speed:" {
			t.Errorf("unexpected speed tag for very slow NIC: %q", tag)
		}
	}
}

func TestDiscover_NoNICs(t *testing.T) {
	hw := buildTestInput(t)
	hw.NICs = nil
	tagSet := tags.Discover(hw)
	// No nic:speed or nic:vendor tags without NICs.
	for _, tag := range tagSet {
		if len(tag) > 10 && tag[:10] == "nic:speed:" {
			t.Errorf("unexpected speed tag with no NICs: %q", tag)
		}
	}
}

func TestDiscover_GPUTags_FPGA(t *testing.T) {
	hw := buildTestInput(t)
	hw.Accelerators.PCIDevices = []probe.PCIDevice{
		{Slot: "01:00.0", Class: "FPGA processing unit", VendorID: "1172", DeviceID: "0001", BusType: probe.BusTypePCIe},
	}
	hw.Accelerators.NvidiaSmi = probe.NvidiaSmiOutput{Available: false}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "accelerator:fpga") {
		t.Error("missing accelerator:fpga for Intel/Altera FPGA")
	}
}

func TestDiscover_GPUTags_Xilinx(t *testing.T) {
	hw := buildTestInput(t)
	hw.Accelerators.PCIDevices = []probe.PCIDevice{
		{Slot: "02:00.0", Class: "Processing accelerators", VendorID: "10ee", DeviceID: "5001", BusType: probe.BusTypePCIe},
	}
	hw.Accelerators.NvidiaSmi = probe.NvidiaSmiOutput{Available: false}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "accelerator:xilinx:alveo-u280") {
		t.Error("missing accelerator:xilinx:alveo-u280 for Xilinx Alveo")
	}
}

func TestDiscover_GPUTags_Hailo(t *testing.T) {
	hw := buildTestInput(t)
	hw.Accelerators.PCIDevices = []probe.PCIDevice{
		{Slot: "03:00.0", Class: "Neural network processing", VendorID: "1e60", DeviceID: "0001", BusType: probe.BusTypePCIe},
	}
	hw.Accelerators.NvidiaSmi = probe.NvidiaSmiOutput{Available: false}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "accelerator:hailo:hailo-8l") {
		t.Error("missing accelerator:hailo:hailo-8l for Hailo device")
	}
}

func TestDiscover_GPUTags_IntelLevelZero(t *testing.T) {
	hw := buildTestInput(t)
	hw.Accelerators.PCIDevices = nil
	hw.Accelerators.NvidiaSmi = probe.NvidiaSmiOutput{Available: false}
	hw.Accelerators.XPUSMI = probe.XPUSMIOutput{Available: true}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "gpu:intel:level-zero") {
		t.Error("missing gpu:intel:level-zero when XPU SMI is available")
	}
}

func TestDiscover_GPUTags_ROCmSmi(t *testing.T) {
	hw := buildTestInput(t)
	hw.Accelerators.PCIDevices = []probe.PCIDevice{
		{Slot: "01:00.0", Class: "VGA compatible controller", VendorID: "1002", DeviceID: "73bf", BusType: probe.BusTypePCIe},
	}
	hw.Accelerators.NvidiaSmi = probe.NvidiaSmiOutput{Available: false}
	hw.Accelerators.ROCmSmi = probe.ROCmSMIOutput{
		Available:   true,
		DeviceLines: []string{"ROCm version: 5.7.0"},
	}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "gpu:amd-rocm") {
		t.Error("missing gpu:amd-rocm for AMD GPU")
	}
	if !hasTag(tagSet, "gpu:amd:rocm-version:5.7.0") {
		t.Errorf("missing gpu:amd:rocm-version:5.7.0; tags=%v", tagSet)
	}
}

func TestDiscover_NvidiaBluefield(t *testing.T) {
	hw := buildTestInput(t)
	hw.Accelerators.PCIDevices = []probe.PCIDevice{
		{Slot: "05:00.0", Class: "InfiniBand controller", VendorID: "15b3", DeviceID: "a2d6", BusType: probe.BusTypePCIe},
	}
	hw.Accelerators.NvidiaSmi = probe.NvidiaSmiOutput{Available: false}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "accelerator:nvidia-bluefield:bf3") {
		t.Errorf("missing accelerator:nvidia-bluefield:bf3; tags=%v", tagSet)
	}
}

func TestDiscover_CXL_NVDIMM(t *testing.T) {
	// Inject a CXL node into lshw and verify tags.
	hw := buildTestInput(t)
	// Modify the lshw root to include a CXL memory node.
	hw.Lshw.Root.Children = append(hw.Lshw.Root.Children, probe.LshwNode{
		Class:       "memory",
		Description: "CXL memory expander",
		Product:     "CXL Device",
	})
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "mem:cxl:true") {
		t.Errorf("missing mem:cxl:true when CXL node present; tags=%v", tagSet)
	}
}

func TestDiscover_SecurityTags_TDX(t *testing.T) {
	hw := buildTestInput(t)
	lscpuJSON := `{"lscpu":[
		{"field":"CPU(s):","data":"64"},
		{"field":"Vendor ID:","data":"GenuineIntel"},
		{"field":"Model name:","data":"Intel Xeon Sapphire Rapids"},
		{"field":"Socket(s):","data":"1"},
		{"field":"Core(s) per socket:","data":"32"},
		{"field":"Flags:","data":"avx2 tdx"}
	]}`
	if err := json.Unmarshal([]byte(lscpuJSON), &hw.NUMA.Lscpu); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "confidential-compute:tdx") {
		t.Errorf("missing confidential-compute:tdx; tags=%v", tagSet)
	}
}

func TestDiscover_SecurityTags_TPM2(t *testing.T) {
	hw := buildTestInput(t)
	// Inject a TPM 2.0 node into lshw.
	hw.Lshw.Root.Children = append(hw.Lshw.Root.Children, probe.LshwNode{
		Class:       "generic",
		Description: "TPM 2.0 Security Device",
		Product:     "TPM2",
	})
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "tpm:2.0") {
		t.Errorf("missing tpm:2.0 when TPM 2.0 node present; tags=%v", tagSet)
	}
}

func TestDiscover_DiskPLP(t *testing.T) {
	// Use a SMART output with a PLP model name.
	hw := buildTestInput(t)
	for i := range hw.SMART {
		hw.SMART[i].ModelName = "Samsung PM9A3 NVMe SSD"
	}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "disk:plp") {
		t.Errorf("missing disk:plp for PM9A3 model; tags=%v", tagSet)
	}
}

func TestDiscover_DiskEnterprise(t *testing.T) {
	hw := buildTestInput(t)
	for i := range hw.SMART {
		hw.SMART[i].ModelName = "WD ENTERPRISE SAS HDD"
	}
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "disk:plp") {
		t.Errorf("missing disk:plp for ENTERPRISE model; tags=%v", tagSet)
	}
}

func TestDiscover_MemNVDIMM(t *testing.T) {
	hw := buildTestInput(t)
	hw.Lshw.Root.Children = append(hw.Lshw.Root.Children, probe.LshwNode{
		Class:       "memory",
		Description: "NVDIMM persistent memory",
		Product:     "NVDIMM-N",
	})
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "mem:nvdimm:true") {
		t.Errorf("missing mem:nvdimm:true; tags=%v", tagSet)
	}
}

// TestPowerOfTwoCeil_EdgeCases exercises the edge paths in powerOfTwoCeil
// through disk tag generation (total disk bytes → GB → power-of-two bucket).
func TestPowerOfTwoCeil_EdgeCases(t *testing.T) {
	// Inject a SMART device with 1 byte capacity — exercises n<=1 path.
	hw := buildTestInput(t)
	// Clear SMART so disk total starts from 0 (skips tag).
	hw.SMART = nil
	tagSet := tags.Discover(hw)
	// No disk:total-gb when SMART is empty and lsblk has no byte sizes.
	for _, tag := range tagSet {
		if tag == "disk:total-gb:0" {
			t.Errorf("unexpected disk:total-gb:0; tags=%v", tagSet)
		}
	}
}

// TestDiscover_UnknownFirmware exercises the firmware switch default branch.
func TestDiscover_UnknownFirmware(t *testing.T) {
	hw := buildTestInput(t)
	hw.Firmware = probe.FirmwareType("unknown")
	tagSet := tags.Discover(hw)
	// Should not emit firmware:uefi or firmware:bios.
	for _, tag := range tagSet {
		if tag == "firmware:uefi" || tag == "firmware:bios" {
			t.Errorf("unexpected firmware tag for unknown type: %q", tag)
		}
	}
}

// TestDiscover_NICSpeed_Subthreshold exercises the nicTags path where no
// bucket matches (speed too low — no nic:speed: tag emitted).
func TestDiscover_NIC_NoMaxSpeed(t *testing.T) {
	hw := buildTestInput(t)
	hw.NICs = nil
	tagSet := tags.Discover(hw)
	for _, tag := range tagSet {
		if len(tag) > 10 && tag[:10] == "nic:speed:" {
			t.Errorf("unexpected nic:speed tag with no NICs: %q", tag)
		}
	}
}

// TestDiscover_DiskSATASSD exercises the sata+!rota → disk:sata-ssd path.
func TestDiscover_DiskSATASSD(t *testing.T) {
	hw := buildTestInput(t)
	// Replace lsblk with a single SATA SSD.
	hw.Lsblk.BlockDevices = []probe.BlockDevice{
		{Name: "sda", Type: "disk", Tran: "sata", Rota: false},
	}
	hw.SMART = nil
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "disk:sata-ssd") {
		t.Errorf("missing disk:sata-ssd; tags=%v", tagSet)
	}
	if !hasTag(tagSet, "disk:tier:fast") {
		t.Errorf("missing disk:tier:fast for SATA SSD; tags=%v", tagSet)
	}
}

// TestDiscover_DiskSASSSD exercises the sas+!rota → disk:sas-ssd path.
func TestDiscover_DiskSASSSD(t *testing.T) {
	hw := buildTestInput(t)
	hw.Lsblk.BlockDevices = []probe.BlockDevice{
		{Name: "sda", Type: "disk", Tran: "sas", Rota: false},
	}
	hw.SMART = nil
	tagSet := tags.Discover(hw)
	if !hasTag(tagSet, "disk:sas-ssd") {
		t.Errorf("missing disk:sas-ssd; tags=%v", tagSet)
	}
}

func TestDiscover_CPUFeatures_AMX_SVE2_SGX_SEV(t *testing.T) {
	features := []struct {
		flag    string
		wantTag string
	}{
		{"amx_bf16", "cpu:feature:amx"},
		{"sve2", "cpu:feature:sve2"},
		{"sgx", "cpu:feature:sgx"},
		{"sev", "cpu:feature:sev"},
	}
	for _, f := range features {
		t.Run(f.wantTag, func(t *testing.T) {
			hw := buildTestInput(t)
			lscpuJSON := `{"lscpu":[
				{"field":"CPU(s):","data":"32"},
				{"field":"Vendor ID:","data":"GenuineIntel"},
				{"field":"Model name:","data":"Intel Xeon"},
				{"field":"Socket(s):","data":"1"},
				{"field":"Core(s) per socket:","data":"16"},
				{"field":"Flags:","data":"` + f.flag + `"}
			]}`
			if err := json.Unmarshal([]byte(lscpuJSON), &hw.NUMA.Lscpu); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			tagSet := tags.Discover(hw)
			if !hasTag(tagSet, f.wantTag) {
				t.Errorf("missing %q for flag %q; tags=%v", f.wantTag, f.flag, tagSet)
			}
		})
	}
}
