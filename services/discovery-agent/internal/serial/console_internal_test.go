//go:build noxdp

package serial

import (
	"bufio"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// noopConfigure stands in for termios setup on regular-file fixtures.
func noopConfigure(*os.File, int) error { return nil }

// readLines returns the non-empty lines written to path.
func readLines(t *testing.T, path string) []string {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open fixture: %v", err)
	}
	defer func() { _ = f.Close() }()
	var out []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if sc.Text() != "" {
			out = append(out, sc.Text())
		}
	}
	return out
}

// TestConsoleWrite checks JSON event framing, nil-field omission, and drop behaviour.
func TestConsoleWrite(t *testing.T) {
	tests := []struct {
		name       string
		configure  func(*os.File, int) error
		devPath    func(t *testing.T) string
		level, msg string
		fields     any
		wantLines  int
		wantKeys   []string
		noKeys     []string
	}{
		{
			name:      "event with fields",
			configure: noopConfigure,
			devPath:   func(t *testing.T) string { return filepath.Join(t.TempDir(), "tty") },
			level:     "info", msg: "hello", fields: map[string]any{"node": "n1"},
			wantLines: 1, wantKeys: []string{"time", "level", "msg", "fields"},
		},
		{
			name:      "nil fields omitted",
			configure: noopConfigure,
			devPath:   func(t *testing.T) string { return filepath.Join(t.TempDir(), "tty") },
			level:     "warn", msg: "no fields", fields: nil,
			wantLines: 1, wantKeys: []string{"level", "msg"}, noKeys: []string{"fields"},
		},
		{
			name:      "missing device drops event",
			configure: noopConfigure,
			devPath:   func(t *testing.T) string { return filepath.Join(t.TempDir(), "absent", "tty") },
			level:     "info", msg: "lost",
			wantLines: 0,
		},
		{
			name:      "configure failure drops event",
			configure: func(*os.File, int) error { return errors.New("ENOTTY") },
			devPath:   func(t *testing.T) string { return filepath.Join(t.TempDir(), "tty") },
			level:     "error", msg: "lost",
			wantLines: 0,
		},
		{
			name:      "unmarshalable fields dropped",
			configure: noopConfigure,
			devPath:   func(t *testing.T) string { return filepath.Join(t.TempDir(), "tty") },
			level:     "error", msg: "bad", fields: make(chan int),
			wantLines: 0,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			path := tc.devPath(t)
			// The device is opened without O_CREATE, so the fixture must exist for the
			// open to succeed and the drop case under test to be reached. The
			// missing-directory case fails this write silently, which is intended.
			_ = os.WriteFile(path, nil, 0o600)
			c := &Console{devPath: path, configure: tc.configure}
			t.Cleanup(c.Close)

			c.write(tc.level, tc.msg, tc.fields)
			c.Close()

			// Missing-device case: the parent directory does not exist, so the
			// file cannot be read back; a clean read of zero lines is the result.
			var lines []string
			if _, err := os.Stat(path); err == nil {
				lines = readLines(t, path)
			}
			if len(lines) != tc.wantLines {
				t.Fatalf("lines = %d, want %d: %v", len(lines), tc.wantLines, lines)
			}
			if tc.wantLines == 0 {
				return
			}

			var m map[string]any
			if err := json.Unmarshal([]byte(lines[0]), &m); err != nil {
				t.Fatalf("event is not JSON: %v: %s", err, lines[0])
			}
			for _, k := range tc.wantKeys {
				if _, ok := m[k]; !ok {
					t.Errorf("key %q missing from %s", k, lines[0])
				}
			}
			for _, k := range tc.noKeys {
				if _, ok := m[k]; ok {
					t.Errorf("key %q should be omitted from %s", k, lines[0])
				}
			}
			if m["msg"] != tc.msg || m["level"] != tc.level {
				t.Errorf("level/msg = %v/%v, want %v/%v", m["level"], m["msg"], tc.level, tc.msg)
			}
		})
	}
}

// TestConsoleReopenAfterClose verifies Close releases the handle and the next write reopens it.
func TestConsoleReopenAfterClose(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tty")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	c := &Console{devPath: path, configure: noopConfigure}

	c.write("info", "first", nil)
	c.Close()
	c.Close() // second Close must be a no-op

	c.write("info", "second", nil)
	c.Close()

	// A regular-file fixture is rewritten from offset 0 on reopen (a tty has no
	// such position), so the observable proof of reopen is that the second
	// event landed and the first was overwritten.
	lines := readLines(t, path)
	if len(lines) != 1 || !strings.Contains(lines[0], `"second"`) {
		t.Fatalf("lines = %v, want single second event", lines)
	}
}

// TestWriteEventUsesDefaultConsole routes the package-level WriteEvent to a fixture device.
func TestWriteEventUsesDefaultConsole(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tty")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	orig := defaultConsole
	defaultConsole = &Console{devPath: path, configure: noopConfigure}
	t.Cleanup(func() {
		defaultConsole.Close()
		defaultConsole = orig
	})

	WriteEvent("info", "via default", map[string]any{"k": "v"})
	defaultConsole.Close()

	lines := readLines(t, path)
	if len(lines) != 1 || !strings.Contains(lines[0], `"via default"`) {
		t.Fatalf("WriteEvent lines = %v", lines)
	}
}

// TestConsoleSetup checks the real defaults point at ttyS0 and 115200 baud.
func TestConsoleSetup(t *testing.T) {
	if defaultConsole.devPath != "/dev/ttyS0" {
		t.Errorf("default devPath = %q, want /dev/ttyS0", defaultConsole.devPath)
	}
	if defaultConsole.configure == nil {
		t.Error("default configure is nil")
	}
	if baudRate != 115200 {
		t.Errorf("baudRate = %d, want 115200", baudRate)
	}
}
