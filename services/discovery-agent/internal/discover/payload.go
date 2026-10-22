//go:build noxdp

package discover

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

// Input is the probe output a discovery Request is built from.
type Input struct {
	MAC          string
	Hostname     string
	DMI          probe.DMIInfo
	Firmware     probe.FirmwareType
	Lshw         json.RawMessage
	Lsblk        json.RawMessage
	NICs         []probe.NICInfo
	SMART        []probe.SmartOutput
	NUMA         probe.NUMAProbeResult
	Accelerators probe.AcceleratorProbeResult
	HardwareTags []string
}

// pciVendors maps PCI vendor IDs to the hub's accelerator vendor names.
var pciVendors = map[string]string{
	"10de": "nvidia",
	"1002": "amd",
	"8086": "intel",
	"1da3": "habana",
}

// BuildRequest converts raw probe output into the hub-shaped discovery Request.
// It fails loudly when a field the hub requires is missing or malformed, so a
// bad probe never reaches the api-manager as an opaque 422.
func BuildRequest(in Input) (Request, error) {
	if in.DMI.ProductUUID == "" {
		return Request{}, fmt.Errorf("discover: DMI product UUID is empty")
	}
	if in.MAC == "" {
		return Request{}, fmt.Errorf("discover: primary NIC MAC is empty")
	}
	if in.Firmware != probe.FirmwareUEFI && in.Firmware != probe.FirmwareBIOS {
		return Request{}, fmt.Errorf("discover: unknown firmware type %q", in.Firmware)
	}
	lshw, err := requireObject("lshw", in.Lshw)
	if err != nil {
		return Request{}, err
	}
	lsblk, err := requireObject("lsblk", in.Lsblk)
	if err != nil {
		return Request{}, err
	}

	return Request{
		DMIUUID:         in.DMI.ProductUUID,
		PrimaryNICMAC:   in.MAC,
		FirmwareType:    string(in.Firmware),
		Hostname:        in.Hostname,
		SysVendor:       in.DMI.SysVendor,
		ProductName:     in.DMI.ProductName,
		BIOSVersion:     in.DMI.BIOSVersion,
		LshwJSON:        lshw,
		LsblkJSON:       lsblk,
		SmartAttributes: smartAttributes(in.SMART),
		Nics:            nicInfos(in.NICs, in.MAC),
		NumaTopology:    numaTopology(in.NUMA),
		Accelerators:    accelerators(in.Accelerators.PCIDevices),
		HardwareTags:    nonNilTags(in.HardwareTags),
	}, nil
}

// requireObject returns raw unchanged when it is nil or a JSON object; any other
// JSON shape is an error because the hub types these fields as objects.
func requireObject(name string, raw json.RawMessage) (json.RawMessage, error) {
	if raw == nil {
		return nil, nil
	}
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || trimmed[0] != '{' || !json.Valid(trimmed) {
		return nil, fmt.Errorf("discover: %s output is not a JSON object", name)
	}
	return trimmed, nil
}

// smartAttributes flattens every disk's ATA SMART table, tagging rows with the disk name.
func smartAttributes(disks []probe.SmartOutput) []SmartAttribute {
	out := make([]SmartAttribute, 0)
	for _, d := range disks {
		for _, a := range d.ATASmartAttributes.Table {
			raw := int64(a.Raw.Value)
			out = append(out, SmartAttribute{
				Device:   d.Device.Name,
				ID:       a.ID,
				Name:     a.Name,
				Value:    a.Value,
				Worst:    a.Worst,
				Thresh:   a.Thresh,
				RawValue: &raw,
			})
		}
	}
	return out
}

// nicInfos maps enumerated NICs; speed is omitted when the kernel reports it unknown (-1).
func nicInfos(nics []probe.NICInfo, primaryMAC string) []NicInfo {
	out := make([]NicInfo, 0, len(nics))
	for _, n := range nics {
		if n.Address == "" {
			continue
		}
		info := NicInfo{
			MAC:       strings.ToLower(n.Address),
			PCISlot:   n.PCISlot,
			IsPrimary: strings.EqualFold(n.Address, primaryMAC),
			Name:      n.Name,
		}
		if n.SpeedMb >= 0 {
			speed := n.SpeedMb
			info.SpeedMbps = &speed
		}
		out = append(out, info)
	}
	return out
}

// numaTopology returns nil when numactl reported no NUMA nodes, so the hub's
// optional field is omitted instead of carrying an invalid nodes=0 topology.
func numaTopology(n probe.NUMAProbeResult) *NumaTopology {
	nodes := n.Numactl.Available
	if nodes < len(n.Numactl.NodeCPUs) {
		nodes = len(n.Numactl.NodeCPUs)
	}
	if nodes < 1 {
		return nil
	}
	topo := &NumaTopology{
		Nodes:    nodes,
		CPUMap:   make(map[string][]int, len(n.Numactl.NodeCPUs)),
		MemoryMB: make(map[string]int64, len(n.Numactl.NodeMem)),
	}
	for id, cpus := range n.Numactl.NodeCPUs {
		topo.CPUMap[strconv.Itoa(id)] = append([]int{}, cpus...)
	}
	for id, mem := range n.Numactl.NodeMem {
		topo.MemoryMB[strconv.Itoa(id)] = mem
	}
	return topo
}

// accelerators maps lspci GPU/accelerator entries to the hub shape.
// External (eGPU) devices are PCIe-tunnelled, so they report as pcie; mediated
// (vGPU) devices have no physical bus and report as embedded.
func accelerators(devs []probe.PCIDevice) []AcceleratorInfo {
	out := make([]AcceleratorInfo, 0, len(devs))
	for _, d := range devs {
		vendor, ok := pciVendors[strings.ToLower(d.VendorID)]
		if !ok {
			vendor = "other"
		}
		var bus string
		switch d.BusType {
		case probe.BusTypeIntegrated:
			bus = "integrated"
		case probe.BusTypeMediated:
			bus = "embedded"
		default:
			bus = "pcie"
		}
		out = append(out, AcceleratorInfo{
			BusType:    bus,
			Vendor:     vendor,
			Model:      d.Device,
			PCIAddress: d.Slot,
		})
	}
	return out
}

// nonNilTags guarantees the hub receives a JSON array rather than null.
func nonNilTags(tags []string) []string {
	if tags == nil {
		return []string{}
	}
	return tags
}
