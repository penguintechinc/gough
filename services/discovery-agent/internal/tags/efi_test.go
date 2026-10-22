//go:build noxdp

// White-box tests for the EFI variable reading helpers.
// These tests use a temp directory to simulate the efivars filesystem.
package tags

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadEFIVarFromFS_Found(t *testing.T) {
	dir := t.TempDir()
	// Simulate SecureBoot-{GUID} file: 4-byte attributes + 1-byte value (1 = enabled).
	guid := "8be4df61-93ca-11d2-aa0d-00e098032b8c"
	data := []byte{0x06, 0x00, 0x00, 0x00, 0x01} // attrs + SecureBoot=1
	if err := os.WriteFile(filepath.Join(dir, "SecureBoot-"+guid), data, 0644); err != nil {
		t.Fatal(err)
	}

	got, err := readEFIVarFromFS(dir, "SecureBoot")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 5 || got[4] != 1 {
		t.Errorf("unexpected data: %v", got)
	}
}

func TestReadEFIVarFromFS_NotFound(t *testing.T) {
	dir := t.TempDir()
	// Directory exists but no matching file.
	_, err := readEFIVarFromFS(dir, "SecureBoot")
	if err == nil {
		t.Fatal("expected error when var not found, got nil")
	}
}

func TestReadEFIVarFromFS_NoDir(t *testing.T) {
	_, err := readEFIVarFromFS("/nonexistent/path/to/efivars", "SecureBoot")
	if err == nil {
		t.Fatal("expected error for non-existent directory, got nil")
	}
}

func TestReadEFIVarFromFS_UnreadableFile(t *testing.T) {
	dir := t.TempDir()
	guid := "8be4df61-93ca-11d2-aa0d-00e098032b8c"
	fpath := filepath.Join(dir, "SecureBoot-"+guid)
	if err := os.WriteFile(fpath, []byte{0x01}, 0000); err != nil {
		t.Fatal(err)
	}
	// Root can always read — skip on root-running environments.
	if os.Getuid() == 0 {
		t.Skip("running as root; permission test not meaningful")
	}
	_, err := readEFIVarFromFS(dir, "SecureBoot")
	if err == nil {
		t.Fatal("expected error for unreadable file, got nil")
	}
}

func TestReadEFISecureBoot_EnabledViaTempFS(t *testing.T) {
	// Override the EFI vars path by calling readEFIVarFromFS directly.
	dir := t.TempDir()
	guid := "8be4df61-93ca-11d2-aa0d-00e098032b8c"
	// 4-byte attributes + 1-byte value where value=1 means enabled.
	if err := os.WriteFile(filepath.Join(dir, "SecureBoot-"+guid), []byte{0, 0, 0, 0, 1}, 0644); err != nil {
		t.Fatal(err)
	}
	data, err := readEFIVarFromFS(dir, "SecureBoot")
	if err != nil {
		t.Fatalf("readEFIVarFromFS: %v", err)
	}
	if len(data) < 5 {
		t.Fatalf("short data: %v", data)
	}
	if data[4] != 1 {
		t.Errorf("expected SecureBoot=1, got %d", data[4])
	}
}

func TestReadEFISecureBoot_Disabled(t *testing.T) {
	dir := t.TempDir()
	guid := "8be4df61-93ca-11d2-aa0d-00e098032b8c"
	if err := os.WriteFile(filepath.Join(dir, "SecureBoot-"+guid), []byte{0, 0, 0, 0, 0}, 0644); err != nil {
		t.Fatal(err)
	}
	data, err := readEFIVarFromFS(dir, "SecureBoot")
	if err != nil {
		t.Fatalf("readEFIVarFromFS: %v", err)
	}
	if data[4] != 0 {
		t.Errorf("expected SecureBoot=0, got %d", data[4])
	}
}
