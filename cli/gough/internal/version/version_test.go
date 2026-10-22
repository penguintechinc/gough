//go:build noxdp

package version

import (
	"strings"
	"testing"
)

func TestString(t *testing.T) {
	s := String()
	if !strings.Contains(s, "dev") {
		t.Errorf("String() = %q; want 'dev' in version", s)
	}
	if !strings.Contains(s, "commit") {
		t.Errorf("String() = %q; want 'commit' keyword", s)
	}
}

func TestInfo(t *testing.T) {
	info := Info()
	expected := []string{"version", "commit", "build_date", "go_version"}
	for _, key := range expected {
		if _, ok := info[key]; !ok {
			t.Errorf("Info() missing key %q", key)
		}
	}
	if info["version"] != Version {
		t.Errorf("Info()[version] = %q; want %q", info["version"], Version)
	}
}
