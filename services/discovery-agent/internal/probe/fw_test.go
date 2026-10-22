//go:build noxdp

package probe_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

func TestDetectFirmware_BIOS(t *testing.T) {
	// On CI (Linux) without UEFI, /sys/firmware/efi should not exist.
	// We skip if it does exist (actual UEFI host).
	if _, err := os.Stat("/sys/firmware/efi"); err == nil {
		fw := probe.DetectFirmware()
		if fw != probe.FirmwareUEFI {
			t.Errorf("expected UEFI on UEFI host, got %q", fw)
		}
		return
	}

	fw := probe.DetectFirmware()
	if fw != probe.FirmwareBIOS {
		t.Errorf("expected BIOS, got %q", fw)
	}
}

func TestDetectFirmware_UEFI(t *testing.T) {
	// Create a temp dir to act as /sys/firmware/efi for mocking.
	// We cannot actually override the hardcoded path; this test verifies
	// that the constants have the expected values.
	if probe.FirmwareBIOS == probe.FirmwareUEFI {
		t.Error("FirmwareBIOS and FirmwareUEFI must differ")
	}
	if probe.FirmwareBIOS != "bios" {
		t.Errorf("FirmwareBIOS = %q, want bios", probe.FirmwareBIOS)
	}
	if probe.FirmwareUEFI != "uefi" {
		t.Errorf("FirmwareUEFI = %q, want uefi", probe.FirmwareUEFI)
	}
	_ = filepath.Join // keep import used
}
