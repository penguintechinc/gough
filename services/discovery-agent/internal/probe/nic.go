//go:build noxdp

package probe

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// NICInfo holds information about a single network interface.
type NICInfo struct {
	Name    string `json:"name"`
	Address string `json:"address"`      // MAC
	SpeedMb int64  `json:"speed_mb"`     // Mbit/s; -1 = unknown
	Carrier bool   `json:"carrier"`
	PCISlot string `json:"pci_slot"`     // from device/uevent PCI_SLOT_NAME, empty if not PCI
	PCIID   string `json:"pci_id"`       // from device/uevent PCI_ID, e.g. "8086:1563"
	Driver  string `json:"driver"`       // from device/uevent DRIVER
}

const netBase = "/sys/class/net"

// EnumerateNICs walks /sys/class/net and returns information for each
// interface that has a valid MAC address. Loop-back and virtual-only
// interfaces without a device symlink are skipped.
func EnumerateNICs() ([]NICInfo, error) {
	entries, err := os.ReadDir(netBase)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", netBase, err)
	}

	var nics []NICInfo
	for _, e := range entries {
		name := e.Name()
		base := filepath.Join(netBase, name)

		mac := readTrimmed(filepath.Join(base, "address"))
		if mac == "" || mac == "00:00:00:00:00:00" {
			continue
		}

		nic := NICInfo{
			Name:    name,
			Address: mac,
			SpeedMb: readInt64(filepath.Join(base, "speed")),
			Carrier: readTrimmed(filepath.Join(base, "carrier")) == "1",
		}

		// PCI info from device/uevent
		uevent := filepath.Join(base, "device", "uevent")
		if data, err := os.ReadFile(uevent); err == nil {
			scanner := bufio.NewScanner(strings.NewReader(string(data)))
			for scanner.Scan() {
				line := scanner.Text()
				switch {
				case strings.HasPrefix(line, "PCI_SLOT_NAME="):
					nic.PCISlot = strings.TrimPrefix(line, "PCI_SLOT_NAME=")
				case strings.HasPrefix(line, "PCI_ID="):
					nic.PCIID = strings.TrimPrefix(line, "PCI_ID=")
				case strings.HasPrefix(line, "DRIVER="):
					nic.Driver = strings.TrimPrefix(line, "DRIVER=")
				}
			}
		}

		nics = append(nics, nic)
	}
	return nics, nil
}

func readTrimmed(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func readInt64(path string) int64 {
	var v int64
	data, err := os.ReadFile(path)
	if err != nil {
		return -1
	}
	if _, err := fmt.Sscanf(strings.TrimSpace(string(data)), "%d", &v); err != nil {
		return -1
	}
	return v
}
