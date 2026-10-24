//go:build noxdp

package probe

import "os"

// FirmwareType classifies the boot firmware.
type FirmwareType string

const (
	FirmwareUEFI FirmwareType = "uefi"
	FirmwareBIOS FirmwareType = "bios"
)

// efiDir is the sysfs marker for UEFI runtime services; a variable for tests.
var efiDir = "/sys/firmware/efi"

// DetectFirmware returns FirmwareUEFI if /sys/firmware/efi exists, otherwise
// FirmwareBIOS. The directory presence is the canonical Linux way to detect
// UEFI runtime services.
func DetectFirmware() FirmwareType {
	if _, err := os.Stat(efiDir); err == nil {
		return FirmwareUEFI
	}
	return FirmwareBIOS
}
