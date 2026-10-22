//go:build noxdp && linux

package serial

import (
	"os"
	"path/filepath"
	"testing"
)

// TestSetSerial exercises termios configuration on a pty master (a real tty) and
// on a regular file (not a tty, so the ioctl must fail).
func TestSetSerial(t *testing.T) {
	tests := []struct {
		name    string
		open    func(t *testing.T) *os.File
		baud    int
		wantErr bool
	}{
		{
			name: "pty accepts 115200",
			open: func(t *testing.T) *os.File {
				f, err := os.OpenFile("/dev/ptmx", os.O_RDWR, 0)
				if err != nil {
					t.Skipf("no pty master available: %v", err)
				}
				return f
			},
			baud: 115200,
		},
		{
			name: "pty accepts 9600",
			open: func(t *testing.T) *os.File {
				f, err := os.OpenFile("/dev/ptmx", os.O_RDWR, 0)
				if err != nil {
					t.Skipf("no pty master available: %v", err)
				}
				return f
			},
			baud: 9600,
		},
		{
			name: "unknown baud falls back to 115200 without error",
			open: func(t *testing.T) *os.File {
				f, err := os.OpenFile("/dev/ptmx", os.O_RDWR, 0)
				if err != nil {
					t.Skipf("no pty master available: %v", err)
				}
				return f
			},
			baud: 1234,
		},
		{
			name: "regular file is not a tty",
			open: func(t *testing.T) *os.File {
				p := filepath.Join(t.TempDir(), "plain")
				if err := os.WriteFile(p, nil, 0o600); err != nil {
					t.Fatal(err)
				}
				f, err := os.OpenFile(p, os.O_RDWR, 0)
				if err != nil {
					t.Fatal(err)
				}
				return f
			},
			baud:    115200,
			wantErr: true,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			f := tc.open(t)
			defer func() { _ = f.Close() }()
			err := setSerial(f, tc.baud)
			if (err != nil) != tc.wantErr {
				t.Fatalf("setSerial error = %v, wantErr %v", err, tc.wantErr)
			}
		})
	}
}
