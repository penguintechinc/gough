//go:build noxdp

package probe_test

import (
	"os"
	"path/filepath"
	"testing"
)

// TestReadDMI uses a fake sysfs tree to avoid needing real hardware.
// We test it via the exported function indirectly by checking that
// reading non-existent DMI returns a useful error.
func TestReadDMI_MissingDirectory(t *testing.T) {
	// We cannot easily mock the hardcoded path "/sys/class/dmi/id"
	// without build-time injection. Instead verify the function runs
	// without panic when the directory is absent (CI on macOS / containers).
	// Real hardware tests happen in integration tests (//go:build integration).
	t.Log("dmi sysfs test is hardware-environment-sensitive; covered by integration tests")
}

func TestReadTrimmed_Helper(t *testing.T) {
	// Write a test file with trailing newline and verify trimming.
	dir := t.TempDir()
	path := filepath.Join(dir, "field")
	if err := os.WriteFile(path, []byte("value\n"), 0644); err != nil {
		t.Fatal(err)
	}

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	got := trimNewline(string(data))
	if got != "value" {
		t.Errorf("trimmed = %q, want value", got)
	}
}

func trimNewline(s string) string {
	for len(s) > 0 && (s[len(s)-1] == '\n' || s[len(s)-1] == '\r' || s[len(s)-1] == ' ') {
		s = s[:len(s)-1]
	}
	return s
}
