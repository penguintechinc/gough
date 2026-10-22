//go:build noxdp

// Package probe collects raw hardware inventory via local system interfaces.
package probe

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// DMIInfo holds values read from /sys/class/dmi/id/.
type DMIInfo struct {
	ProductUUID string `json:"product_uuid"`
	SysVendor   string `json:"sys_vendor"`
	ProductName string `json:"product_name"`
	BIOSVersion string `json:"bios_version"`
}

const dmiBase = "/sys/class/dmi/id"

// ReadDMI reads DMI fields from sysfs. Fields that cannot be read are left
// as empty strings; only a wholesale directory absence returns an error.
func ReadDMI() (DMIInfo, error) {
	if _, err := os.Stat(dmiBase); os.IsNotExist(err) {
		return DMIInfo{}, fmt.Errorf("dmi sysfs unavailable: %w", err)
	}

	read := func(field string) string {
		data, err := os.ReadFile(filepath.Join(dmiBase, field))
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	}

	return DMIInfo{
		ProductUUID: read("product_uuid"),
		SysVendor:   read("sys_vendor"),
		ProductName: read("product_name"),
		BIOSVersion: read("bios_version"),
	}, nil
}
