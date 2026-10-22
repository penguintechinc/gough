//go:build noxdp

package probe

import (
	"bufio"
	"context"
	"encoding/json"
	"encoding/xml"
	"strings"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
)

// AcceleratorBusType classifies how the accelerator is attached to the system.
type AcceleratorBusType string

const (
	BusTypeIntegrated AcceleratorBusType = "integrated"
	BusTypePCIe       AcceleratorBusType = "pcie"
	BusTypeExternal   AcceleratorBusType = "external"
	BusTypeMediated   AcceleratorBusType = "mediated"
)

// PCIDevice is a single line from lspci -nn output.
type PCIDevice struct {
	Slot    string `json:"slot"`
	Class   string `json:"class"`
	Vendor  string `json:"vendor"`
	Device  string `json:"device"`
	VendorID string `json:"vendor_id"` // e.g. "10de"
	DeviceID string `json:"device_id"` // e.g. "2204"
	BusType AcceleratorBusType `json:"bus_type"`
}

// NvidiaSmiGPU represents a single GPU from nvidia-smi -q -x XML output.
type NvidiaSmiGPU struct {
	ProductName      string `xml:"product_name"`
	UUID             string `xml:"uuid"`
	PCIBus           string `xml:"pci>pci_bus_id"`
	CudaVersion      string `xml:"cuda_version"`
	DriverVersion    string `xml:"driver_version"`
	MIGMode          string `xml:"mig_mode>current_mig"`
	ComputeCapMajor  string `xml:"compute_cap_major"`
	ComputeCapMinor  string `xml:"compute_cap_minor"`
}

// NvidiaSmiOutput holds parsed output from nvidia-smi.
type NvidiaSmiOutput struct {
	GPUs []NvidiaSmiGPU
	Available bool
}

// ROCmSMIOutput holds raw output from rocm-smi (best-effort text parse).
type ROCmSMIOutput struct {
	DeviceLines []string `json:"device_lines"`
	Available   bool     `json:"available"`
}

// XPUSMIOutput holds raw output from xpu-smi (Intel GPU).
type XPUSMIOutput struct {
	Raw       string `json:"raw"`
	Available bool   `json:"available"`
}

// AcceleratorProbeResult bundles all accelerator detection results.
type AcceleratorProbeResult struct {
	PCIDevices []PCIDevice     `json:"pci_devices"`
	NvidiaSmi  NvidiaSmiOutput `json:"nvidia_smi"`
	ROCmSmi    ROCmSMIOutput   `json:"rocm_smi"`
	XPUSMI     XPUSMIOutput    `json:"xpu_smi"`
}

// RunAccelerators runs lspci and best-effort vendor tools to enumerate GPUs/accelerators.
func RunAccelerators(ctx context.Context, runner exec.Runner) AcceleratorProbeResult {
	var result AcceleratorProbeResult

	// lspci -nn — mandatory baseline
	if out, err := runner.Run(ctx, "lspci", "-nn"); err == nil {
		result.PCIDevices = parseLspci(string(out))
	}

	// nvidia-smi -q -x — best-effort
	if out, err := runner.Run(ctx, "nvidia-smi", "-q", "-x"); err == nil && len(out) > 0 {
		result.NvidiaSmi = parseNvidiaSmi(out)
	}

	// rocm-smi --showallinfo — best-effort
	if out, err := runner.Run(ctx, "rocm-smi", "--showallinfo"); err == nil && len(out) > 0 {
		result.ROCmSmi = ROCmSMIOutput{
			DeviceLines: strings.Split(string(out), "\n"),
			Available:   true,
		}
	}

	// xpu-smi discovery — best-effort (Intel GPU)
	if out, err := runner.Run(ctx, "xpu-smi", "discovery"); err == nil && len(out) > 0 {
		result.XPUSMI = XPUSMIOutput{Raw: string(out), Available: true}
	}

	return result
}

// parseLspci parses "lspci -nn" text output into PCIDevice entries.
// Sample line:
//   00:02.0 VGA compatible controller [0300]: Intel Corporation [8086:1234] (rev 01)
func parseLspci(text string) []PCIDevice {
	var devices []PCIDevice
	scanner := bufio.NewScanner(strings.NewReader(text))
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}

		// Slot is the first whitespace-delimited token.
		parts := strings.SplitN(line, " ", 2)
		if len(parts) < 2 {
			continue
		}
		slot := parts[0]
		rest := parts[1]

		// Extract vendor:device IDs from last [...] before end of line.
		vendorID, deviceID := extractPCIIDs(rest)

		// Class description is between slot and first [:
		classEnd := strings.Index(rest, "[")
		class := ""
		if classEnd > 0 {
			class = strings.TrimSpace(rest[:classEnd])
		}

		busType := classifyBusType(slot, class, vendorID)

		devices = append(devices, PCIDevice{
			Slot:     slot,
			Class:    class,
			VendorID: vendorID,
			DeviceID: deviceID,
			BusType:  busType,
		})
	}
	return devices
}

// extractPCIIDs extracts vendor_id and device_id from a string like "[8086:1234]".
func extractPCIIDs(s string) (vendorID, deviceID string) {
	for i := len(s) - 1; i >= 0; i-- {
		if s[i] == ']' {
			start := strings.LastIndex(s[:i], "[")
			if start >= 0 {
				id := s[start+1 : i]
				parts := strings.Split(id, ":")
				if len(parts) == 2 {
					return strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1])
				}
			}
			break
		}
	}
	return "", ""
}

// classifyBusType returns the AcceleratorBusType based on PCI slot and class.
// Rules per spec:
//   integrated — display/VGA class, bus 00 (host bridge domain)
//   pcie       — discrete GPU/accelerator class on PCIe bus
//   external   — Thunderbolt / USB domain (bus prefix e.g. "tb")
//   mediated   — virtual function (has .0+ device number indicates SRIOV VF heuristic)
func classifyBusType(slot, class, _ string) AcceleratorBusType {
	cl := strings.ToLower(class)
	busNum := ""
	if parts := strings.Split(slot, ":"); len(parts) >= 1 {
		busNum = parts[0]
	}

	if strings.HasPrefix(slot, "tb") {
		return BusTypeExternal
	}
	// VFs have a non-zero function number and the parent is a PF
	if strings.HasSuffix(slot, ".7") || strings.Contains(slot, ".") {
		fn := slot[strings.LastIndex(slot, ".")+1:]
		if fn != "0" && fn != "" {
			return BusTypeMediated
		}
	}
	if busNum == "00" && (strings.Contains(cl, "vga") || strings.Contains(cl, "display")) {
		return BusTypeIntegrated
	}
	return BusTypePCIe
}

// nvidiaXML is the envelope for nvidia-smi -q -x output.
type nvidiaXML struct {
	XMLName xml.Name       `xml:"nvidia_smi_log"`
	GPUs    []NvidiaSmiGPU `xml:"gpu"`
}

func parseNvidiaSmi(data []byte) NvidiaSmiOutput {
	var envelope nvidiaXML
	if err := xml.Unmarshal(data, &envelope); err != nil {
		return NvidiaSmiOutput{Available: false}
	}
	return NvidiaSmiOutput{GPUs: envelope.GPUs, Available: true}
}

// MarshalJSON produces JSON output for AcceleratorProbeResult including
// the PCIDevices array from lspci and availability flags for each tool.
func (a AcceleratorProbeResult) MarshalJSON() ([]byte, error) {
	type alias struct {
		PCIDevices    []PCIDevice    `json:"pci_devices"`
		NvidiaAvail   bool           `json:"nvidia_available"`
		NvidiaGPUs    []NvidiaSmiGPU `json:"nvidia_gpus,omitempty"`
		ROCmAvail     bool           `json:"rocm_available"`
		IntelXPUAvail bool           `json:"intel_xpu_available"`
	}
	return json.Marshal(alias{
		PCIDevices:    a.PCIDevices,
		NvidiaAvail:   a.NvidiaSmi.Available,
		NvidiaGPUs:    a.NvidiaSmi.GPUs,
		ROCmAvail:     a.ROCmSmi.Available,
		IntelXPUAvail: a.XPUSMI.Available,
	})
}
