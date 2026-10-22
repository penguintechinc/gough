//go:build noxdp

package cmdline

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestParse exercises the file-reading entry point against fixture cmdline files.
func TestParse(t *testing.T) {
	valid := "BOOT_IMAGE=/vmlinuz gough_token=tok gough_primary=https://api.example.com gough_mac=aa:bb:cc:dd:ee:ff\n"

	tests := []struct {
		name      string
		setup     func(t *testing.T) string
		wantErr   string
		wantToken string
	}{
		{
			name: "valid cmdline file",
			setup: func(t *testing.T) string {
				return writeFixture(t, valid)
			},
			wantToken: "tok",
		},
		{
			name: "missing file",
			setup: func(t *testing.T) string {
				return filepath.Join(t.TempDir(), "absent")
			},
			wantErr: "read ",
		},
		{
			name: "file lacks required param",
			setup: func(t *testing.T) string {
				return writeFixture(t, "quiet splash\n")
			},
			wantErr: "gough_token not found",
		},
	}

	orig := cmdlinePath
	t.Cleanup(func() { cmdlinePath = orig })

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cmdlinePath = tc.setup(t)
			p, err := Parse()
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("error = %v, want substring %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if p.Token != tc.wantToken {
				t.Errorf("Token = %q, want %q", p.Token, tc.wantToken)
			}
		})
	}
}

// TestParseLiveProc asserts the production default points at /proc/cmdline.
func TestParseLiveProc(t *testing.T) {
	if cmdlinePath != "/proc/cmdline" {
		t.Fatalf("default cmdlinePath = %q, want /proc/cmdline", cmdlinePath)
	}
	if _, err := os.Stat(cmdlinePath); err != nil {
		t.Skipf("no %s on this host: %v", cmdlinePath, err)
	}
	// On a host the parse may legitimately fail (no gough_ params); only the
	// read path is under test, so accept either outcome but require a
	// wrapped, non-panicking result.
	if _, err := Parse(); err != nil && !strings.Contains(err.Error(), "gough_") {
		if !strings.Contains(err.Error(), "/proc/cmdline") {
			t.Errorf("unexpected error shape: %v", err)
		}
	}
}

// writeFixture writes content to a temp file and returns its path.
func writeFixture(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "cmdline")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	return path
}
