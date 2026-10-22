//go:build noxdp

// Package tags generates the auto-discovered hardware-tag set from probe output.
// Tags are flat "key:value" strings matching the Tag Catalog in the platform spec.
package tags

import (
	"fmt"
	"math"
	"strconv"
	"strings"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

// HardwareInput bundles all probe results consumed by Discover.
type HardwareInput struct {
	Lshw         probe.LshwOutput
	Lsblk        probe.LsblkOutput
	DMI          probe.DMIInfo
	NICs         []probe.NICInfo
	SMART        []probe.SmartOutput
	Firmware     probe.FirmwareType
	NUMA         probe.NUMAProbeResult
	Accelerators probe.AcceleratorProbeResult
}

// Discover generates the full auto-discovered hardware tag set from probe output.
// All tags follow the "key:subkey:value" flat namespace from the spec.
func Discover(hw HardwareInput) []string {
	var tags []string
	tags = append(tags, cpuTags(hw)...)
	tags = append(tags, memTags(hw)...)
	tags = append(tags, diskTags(hw)...)
	tags = append(tags, nicTags(hw)...)
	tags = append(tags, gpuTags(hw)...)
	tags = append(tags, fwTags(hw)...)
	tags = append(tags, securityTags(hw)...)
	return dedup(tags)
}

// ─── CPU ────────────────────────────────────────────────────────────────────

func cpuTags(hw HardwareInput) []string {
	var tags []string

	// Use lscpu for authoritative CPU data.
	lscpu := &hw.NUMA.Lscpu

	vendor := strings.ToLower(lscpu.Get("Vendor ID"))
	switch {
	case strings.Contains(vendor, "genuineintel") || strings.Contains(vendor, "intel"):
		tags = append(tags, "cpu:vendor:intel")
	case strings.Contains(vendor, "authenticamd") || strings.Contains(vendor, "amd"):
		tags = append(tags, "cpu:vendor:amd")
	case strings.Contains(vendor, "arm"):
		tags = append(tags, "cpu:vendor:arm")
	case strings.Contains(vendor, "apple"):
		tags = append(tags, "cpu:vendor:apple")
	}

	// Core/thread/socket counts
	if v := lscpu.Get("CPU(s)"); v != "" {
		tags = append(tags, "cpu:threads:"+v)
	}
	if v := lscpu.Get("Core(s) per socket"); v != "" {
		sockets := 1
		if s := lscpu.Get("Socket(s)"); s != "" {
			if n, err := strconv.Atoi(s); err == nil {
				sockets = n
			}
		}
		if corePer, err := strconv.Atoi(v); err == nil {
			tags = append(tags, fmt.Sprintf("cpu:cores:%d", corePer*sockets))
		}
	}
	if v := lscpu.Get("Socket(s)"); v != "" {
		tags = append(tags, "cpu:sockets:"+v)
	}

	// CPU family / microarch (best-effort from model name)
	modelName := strings.ToLower(lscpu.Get("Model name"))
	family := detectUarch(modelName, vendor)
	if family != "" {
		tags = append(tags, "cpu:family:"+family)
	}

	// CPU flags → feature tags
	flags := strings.Fields(strings.ToLower(lscpu.Get("Flags")))
	flagSet := make(map[string]struct{}, len(flags))
	for _, f := range flags {
		flagSet[f] = struct{}{}
	}

	featureMap := map[string]string{
		"avx2":     "cpu:feature:avx2",
		"avx512f":  "cpu:feature:avx512",
		"amx_bf16": "cpu:feature:amx",
		"sve2":     "cpu:feature:sve2",
		"sgx":      "cpu:feature:sgx",
		"sev":      "cpu:feature:sev",
		"sev_snp":  "cpu:feature:sev-snp",
		"tdx":      "cpu:feature:tdx",
	}
	for flag, tag := range featureMap {
		if _, ok := flagSet[flag]; ok {
			tags = append(tags, tag)
		}
	}

	// ARM CCA — check lshw vendor/feature string
	hw.Lshw.Walk(func(n *probe.LshwNode) {
		if n.Class == "processor" {
			if strings.Contains(strings.ToLower(n.Description), "cca") {
				tags = append(tags, "cpu:feature:cca")
			}
		}
	})

	// Nested virtualisation
	if _, vmx := flagSet["vmx"]; vmx {
		tags = append(tags, "cpu:nested-virt-supported")
	}
	if _, svm := flagSet["svm"]; svm {
		tags = append(tags, "cpu:nested-virt-supported")
	}

	return tags
}

// detectUarch maps CPU model name substrings to spec family names.
func detectUarch(modelName, vendor string) string {
	lv := strings.ToLower(vendor)
	lm := strings.ToLower(modelName)

	if strings.Contains(lv, "intel") {
		switch {
		case strings.Contains(lm, "granite rapids") || strings.Contains(lm, "granite-rapids"):
			return "granite-rapids"
		case strings.Contains(lm, "sapphire rapids") || strings.Contains(lm, "sapphire-rapids"):
			return "sapphire-rapids"
		case strings.Contains(lm, "ice lake"):
			return "ice-lake"
		case strings.Contains(lm, "cascade lake"):
			return "cascade-lake"
		}
	}
	if strings.Contains(lv, "amd") {
		switch {
		case strings.Contains(lm, "genoa") || strings.Contains(lm, "zen 4"):
			return "zen4"
		case strings.Contains(lm, "milan") || strings.Contains(lm, "zen 3"):
			return "zen3"
		case strings.Contains(lm, "rome") || strings.Contains(lm, "zen 2"):
			return "zen2"
		case strings.Contains(lm, "naples") || strings.Contains(lm, "zen 1"):
			return "zen1"
		}
	}
	if strings.Contains(lm, "graviton4") {
		return "graviton4"
	}
	if strings.Contains(lm, "graviton3") {
		return "graviton3"
	}
	return ""
}

// ─── MEMORY ─────────────────────────────────────────────────────────────────

func memTags(hw HardwareInput) []string {
	var tags []string
	lscpu := &hw.NUMA.Lscpu

	// Total physical memory from lshw root node (bytes → GB, power-of-two bucket)
	var totalMemBytes int64
	hw.Lshw.Walk(func(n *probe.LshwNode) {
		if n.Class == "memory" && strings.Contains(strings.ToLower(n.Description), "system memory") {
			if n.Size > totalMemBytes {
				totalMemBytes = n.Size
			}
		}
	})
	if totalMemBytes > 0 {
		gb := powerOfTwoCeil(totalMemBytes / (1024 * 1024 * 1024))
		tags = append(tags, fmt.Sprintf("mem:total-gb:%d", gb))
	}

	// NUMA nodes
	numaNodes := len(hw.NUMA.Numactl.NodeCPUs)
	if numaNodes == 0 {
		numaNodes = 1 // Assume UMA if numactl not available
	}
	tags = append(tags, fmt.Sprintf("mem:numa-nodes:%d", numaNodes))

	// ECC — lscpu has "NUMA node(s)"; ECC is reported in lshw memory nodes
	eccPresent := false
	hw.Lshw.Walk(func(n *probe.LshwNode) {
		if n.Class == "memory" {
			if _, ok := n.Capabilities["ecc"]; ok {
				eccPresent = true
			}
			if strings.Contains(strings.ToLower(n.Description), "ecc") {
				eccPresent = true
			}
		}
	})
	if eccPresent {
		tags = append(tags, "mem:ecc:true")
	} else {
		tags = append(tags, "mem:ecc:false")
	}

	// NVDIMM / CXL — from lshw or lscpu
	hw.Lshw.Walk(func(n *probe.LshwNode) {
		desc := strings.ToLower(n.Description + n.Product)
		if strings.Contains(desc, "nvdimm") {
			tags = append(tags, "mem:nvdimm:true")
		}
		if strings.Contains(desc, "cxl") {
			tags = append(tags, "mem:cxl:true")
		}
	})
	_ = lscpu // lscpu used above; suppress unused var if future code references it

	return tags
}

// powerOfTwoCeil rounds n up to the nearest power of two (for capacity bucketing).
func powerOfTwoCeil(n int64) int64 {
	if n <= 0 {
		return 1
	}
	if n == 1 {
		return 1
	}
	// Find highest set bit position.
	bits := int(math.Ceil(math.Log2(float64(n))))
	v := int64(1) << bits
	if v < n {
		v <<= 1
	}
	return v
}

// ─── DISK ────────────────────────────────────────────────────────────────────

func diskTags(hw HardwareInput) []string {
	var tags []string

	typeSet := map[string]bool{}
	var totalBytes int64

	for _, d := range hw.Lsblk.TopLevelDisks() {
		tran := strings.ToLower(d.Tran)

		switch {
		case tran == "nvme":
			typeSet["disk:nvme"] = true
		case tran == "sata" && !d.Rota:
			typeSet["disk:sata-ssd"] = true
		case tran == "sas" && !d.Rota:
			typeSet["disk:sas-ssd"] = true
		case tran == "sata" && d.Rota:
			typeSet["disk:sata-hdd"] = true
		case tran == "sas" && d.Rota:
			typeSet["disk:sas-hdd"] = true
		}

		// Sum capacity from SMART (more reliable than lsblk human sizes).
		for _, s := range hw.SMART {
			if s.Device.InfoName == "/dev/"+d.Name || s.Device.Name == "/dev/"+d.Name {
				totalBytes += s.UserCapacity.Bytes
			}
		}
	}

	for t := range typeSet {
		tags = append(tags, t)
	}

	if totalBytes > 0 {
		gb := powerOfTwoCeil(totalBytes / (1024 * 1024 * 1024))
		tags = append(tags, fmt.Sprintf("disk:total-gb:%d", gb))
	}

	// PLP (power-loss-protected) from SMART model name patterns
	for _, s := range hw.SMART {
		model := strings.ToUpper(s.ModelName + s.ModelFamily)
		if strings.Contains(model, "PLP") || strings.Contains(model, "ENTERPRISE") ||
			strings.Contains(model, "PM9A3") || strings.Contains(model, "PE8010") {
			tags = append(tags, "disk:plp")
			break
		}
	}

	// Drive tier classification (fast = NVMe / SAS SSD, bulk = HDD)
	if typeSet["disk:nvme"] || typeSet["disk:sas-ssd"] || typeSet["disk:sata-ssd"] {
		tags = append(tags, "disk:tier:fast")
	}
	if typeSet["disk:sata-hdd"] || typeSet["disk:sas-hdd"] {
		tags = append(tags, "disk:tier:bulk")
	}

	return tags
}

// ─── NIC ─────────────────────────────────────────────────────────────────────

var nicSpeedBuckets = []int64{400000, 200000, 100000, 40000, 25000, 10000, 1000, 100}

func nicTags(hw HardwareInput) []string {
	var tags []string
	maxSpeed := int64(-1)
	vendorSet := map[string]bool{}
	sriovPresent := false

	for _, nic := range hw.NICs {
		if nic.SpeedMb > maxSpeed {
			maxSpeed = nic.SpeedMb
		}
		if v := classifyNICVendor(nic); v != "" {
			vendorSet[v] = true
		}
		// SRIOV — check sysfs device/sriov_totalvfs
		// We don't have a direct sysfs read here; use PCIID heuristic for known SR-IOV capable NICs.
		if strings.Contains(strings.ToLower(nic.Driver), "sriov") {
			sriovPresent = true
		}
	}

	// Speed tag — report highest speed present.
	for _, bucket := range nicSpeedBuckets {
		if maxSpeed >= bucket {
			tags = append(tags, fmt.Sprintf("nic:speed:%s", speedLabel(bucket)))
			break
		}
	}

	for v := range vendorSet {
		tags = append(tags, "nic:vendor:"+v)
	}

	if sriovPresent {
		tags = append(tags, "nic:sr-iov")
	}

	// RDMA detection — from lshw class=network capabilities
	hw.Lshw.Walk(func(n *probe.LshwNode) {
		if n.Class != "network" {
			return
		}
		for cap := range n.Capabilities {
			cl := strings.ToLower(cap)
			switch {
			case strings.Contains(cl, "roce"):
				tags = append(tags, "nic:rdma:roce")
			case strings.Contains(cl, "iwarp"):
				tags = append(tags, "nic:rdma:iwarp")
			case strings.Contains(cl, "infiniband"):
				tags = append(tags, "nic:rdma:infiniband")
			}
		}
	})

	return tags
}

func speedLabel(mbps int64) string {
	switch mbps {
	case 400000:
		return "400g"
	case 200000:
		return "200g"
	case 100000:
		return "100g"
	case 40000:
		return "40g"
	case 25000:
		return "25g"
	case 10000:
		return "10g"
	case 1000:
		return "1g"
	default:
		return fmt.Sprintf("%dm", mbps)
	}
}

func classifyNICVendor(nic probe.NICInfo) string {
	vid := strings.ToLower(nic.PCIID)
	switch {
	case strings.HasPrefix(vid, "15b3:"):
		return "mellanox"
	case strings.HasPrefix(vid, "8086:"):
		return "intel-e810" // conservative; could be any Intel NIC
	case strings.HasPrefix(vid, "14e4:"):
		return "broadcom"
	}
	return ""
}

// ─── GPU / ACCELERATORS ──────────────────────────────────────────────────────

func gpuTags(hw HardwareInput) []string {
	var tags []string

	busTypeSet := map[probe.AcceleratorBusType]bool{}
	vendorSet := map[string]bool{}

	for _, dev := range hw.Accelerators.PCIDevices {
		cl := strings.ToLower(dev.Class)
		if !strings.Contains(cl, "vga") && !strings.Contains(cl, "display") &&
			!strings.Contains(cl, "3d") && !strings.Contains(cl, "render") &&
			!strings.Contains(cl, "multimedia") {
			continue
		}

		busTypeSet[dev.BusType] = true

		vid := strings.ToLower(dev.VendorID)
		switch vid {
		case "10de":
			vendorSet["nvidia"] = true
		case "1002":
			vendorSet["amd-rocm"] = true
		case "8086":
			if dev.BusType == probe.BusTypeIntegrated {
				vendorSet["intel-integrated"] = true
			} else {
				vendorSet["intel-discrete"] = true
			}
		}
	}

	for bt := range busTypeSet {
		tags = append(tags, "gpu:"+string(bt))
	}
	for v := range vendorSet {
		tags = append(tags, "gpu:"+v)
	}

	// NVIDIA detailed tags from nvidia-smi
	if hw.Accelerators.NvidiaSmi.Available {
		nvidiaCount := len(hw.Accelerators.NvidiaSmi.GPUs)
		if nvidiaCount > 0 {
			tags = append(tags, fmt.Sprintf("gpu:vendor:nvidia:count:%d", nvidiaCount))
		}
		for _, g := range hw.Accelerators.NvidiaSmi.GPUs {
			if g.ComputeCapMajor != "" && g.ComputeCapMinor != "" {
				tags = append(tags, fmt.Sprintf("gpu:nvidia:cuda-arch:sm_%s%s",
					g.ComputeCapMajor, g.ComputeCapMinor))
			}
			if strings.Contains(strings.ToLower(g.MIGMode), "enabled") ||
				strings.Contains(strings.ToLower(g.MIGMode), "on") {
				tags = append(tags, "gpu:nvidia:mig-capable")
			}
		}
	}

	// Intel Level Zero
	if hw.Accelerators.XPUSMI.Available {
		tags = append(tags, "gpu:intel:level-zero")
	}

	// AMD ROCm version (best-effort parse from rocm-smi output)
	if hw.Accelerators.ROCmSmi.Available {
		for _, line := range hw.Accelerators.ROCmSmi.DeviceLines {
			if strings.Contains(strings.ToLower(line), "rocm version") {
				parts := strings.Fields(line)
				for i, p := range parts {
					if strings.Contains(strings.ToLower(p), "version") && i+1 < len(parts) {
						tags = append(tags, "gpu:amd:rocm-version:"+parts[i+1])
						break
					}
				}
			}
		}
	}

	// FPGA / DPU / NPU / TPU from PCI class codes
	for _, dev := range hw.Accelerators.PCIDevices {
		cl := strings.ToLower(dev.Class)
		switch {
		case strings.Contains(cl, "fpga") || dev.VendorID == "1172" /* Altera/Intel FPGA */:
			tags = append(tags, "accelerator:fpga")
		case strings.Contains(cl, "dpu") || strings.Contains(cl, "smartnic"):
			tags = append(tags, "accelerator:dpu")
		case strings.Contains(cl, "npu") || strings.Contains(cl, "neural"):
			tags = append(tags, "accelerator:npu")
		}
		// NVIDIA BlueField (DPU)
		if dev.VendorID == "15b3" && strings.Contains(strings.ToLower(dev.Class), "infiniband") {
			tags = append(tags, "accelerator:nvidia-bluefield:bf3")
		}
		// Xilinx Alveo
		if dev.VendorID == "10ee" {
			tags = append(tags, "accelerator:xilinx:alveo-u280")
		}
		// Hailo
		if dev.VendorID == "1e60" {
			tags = append(tags, "accelerator:hailo:hailo-8l")
		}
	}

	return tags
}

// ─── FIRMWARE ────────────────────────────────────────────────────────────────

func fwTags(hw HardwareInput) []string {
	var tags []string

	switch hw.Firmware {
	case probe.FirmwareUEFI:
		tags = append(tags, "firmware:uefi")
	case probe.FirmwareBIOS:
		tags = append(tags, "firmware:bios")
	}
	return tags
}

// ─── SECURITY ────────────────────────────────────────────────────────────────

func securityTags(hw HardwareInput) []string {
	var tags []string

	hw.Lshw.Walk(func(n *probe.LshwNode) {
		desc := strings.ToLower(n.Description + n.Product)
		if strings.Contains(desc, "tpm 2") || strings.Contains(desc, "tpm2") {
			tags = append(tags, "tpm:2.0")
		}
		if strings.Contains(desc, "fido") || strings.Contains(desc, "attestation") {
			tags = append(tags, "tpm:fido-attestation")
		}
	})

	// Secure boot from efivarfs
	if secureBoot, err := readEFISecureBoot(); err == nil && secureBoot {
		tags = append(tags, "secureboot:enabled")
	}

	// Confidential compute from CPU feature tags already detected
	lscpu := &hw.NUMA.Lscpu
	flags := strings.ToLower(lscpu.Get("Flags"))
	if strings.Contains(flags, "sev_snp") {
		tags = append(tags, "confidential-compute:sev-snp")
	}
	if strings.Contains(flags, "tdx") {
		tags = append(tags, "confidential-compute:tdx")
	}

	return tags
}

func readEFISecureBoot() (bool, error) {
	// /sys/firmware/efi/efivars/SecureBoot-<GUID>
	// EFI variable format: 4 bytes attributes + 1 byte value (1=enabled).
	const efiVarsPath = "/sys/firmware/efi/efivars"
	data, err := readEFIVarFromFS(efiVarsPath, "SecureBoot")
	if err != nil {
		return false, err
	}
	if len(data) < 5 {
		return false, fmt.Errorf("short SecureBoot var: %d bytes", len(data))
	}
	return data[4] == 1, nil
}

// ─── HELPERS ─────────────────────────────────────────────────────────────────

func dedup(tags []string) []string {
	seen := make(map[string]struct{}, len(tags))
	out := make([]string, 0, len(tags))
	for _, t := range tags {
		if _, ok := seen[t]; !ok {
			seen[t] = struct{}{}
			out = append(out, t)
		}
	}
	return out
}
