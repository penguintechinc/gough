//go:build noxdp

package cmdline_test

import (
	"os"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/cmdline"
)

func TestParseString_AllParams(t *testing.T) {
	input := "BOOT_IMAGE=/vmlinuz gough_token=eyJhbGc.payload.sig gough_primary=https://api.gough.example.com gough_mac=aa:bb:cc:dd:ee:ff quiet"
	p, err := cmdline.ParseString(input)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if p.Token != "eyJhbGc.payload.sig" {
		t.Errorf("token: got %q, want %q", p.Token, "eyJhbGc.payload.sig")
	}
	if p.Primary != "https://api.gough.example.com" {
		t.Errorf("primary: got %q, want %q", p.Primary, "https://api.gough.example.com")
	}
	if p.MAC != "aa:bb:cc:dd:ee:ff" {
		t.Errorf("mac: got %q, want %q", p.MAC, "aa:bb:cc:dd:ee:ff")
	}
}

func TestParseString_MissingToken(t *testing.T) {
	input := "gough_primary=https://api.example.com gough_mac=aa:bb:cc:dd:ee:ff"
	_, err := cmdline.ParseString(input)
	if err == nil {
		t.Fatal("expected error for missing gough_token, got nil")
	}
}

func TestParseString_MissingPrimary(t *testing.T) {
	input := "gough_token=tok123 gough_mac=aa:bb:cc:dd:ee:ff"
	_, err := cmdline.ParseString(input)
	if err == nil {
		t.Fatal("expected error for missing gough_primary, got nil")
	}
}

func TestParseString_MissingMAC(t *testing.T) {
	input := "gough_token=tok123 gough_primary=https://api.example.com"
	_, err := cmdline.ParseString(input)
	if err == nil {
		t.Fatal("expected error for missing gough_mac, got nil")
	}
}

func TestParseString_EmptyCmdline(t *testing.T) {
	_, err := cmdline.ParseString("")
	if err == nil {
		t.Fatal("expected error for empty cmdline, got nil")
	}
}

func TestParseString_ExtraParams(t *testing.T) {
	// Extra kernel params should not cause errors.
	input := "BOOT_IMAGE=/boot/vmlinuz-5.15 root=/dev/sda1 ro quiet splash " +
		"gough_token=mytoken gough_primary=https://primary.example.com gough_mac=de:ad:be:ef:00:01 " +
		"console=ttyS0,115200 rd.neednet=1"
	p, err := cmdline.ParseString(input)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if p.Token != "mytoken" {
		t.Errorf("token: got %q", p.Token)
	}
	if p.Primary != "https://primary.example.com" {
		t.Errorf("primary: got %q", p.Primary)
	}
	if p.MAC != "de:ad:be:ef:00:01" {
		t.Errorf("mac: got %q", p.MAC)
	}
}

func TestParseString_NoGoughParams(t *testing.T) {
	input := "BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro quiet"
	_, err := cmdline.ParseString(input)
	if err == nil {
		t.Fatal("expected error when no gough params present")
	}
}

func TestParseString_TokenWithEquals(t *testing.T) {
	// JWT tokens themselves contain dots but not equals in the standard encoding.
	// Ensure the prefix trim stops at the first '=' only.
	input := "gough_token=header.payload.sig gough_primary=https://p.example.com gough_mac=11:22:33:44:55:66"
	p, err := cmdline.ParseString(input)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if p.Token != "header.payload.sig" {
		t.Errorf("token: got %q", p.Token)
	}
}

// TestParse_FileMissing tests that Parse() returns a meaningful error when
// /proc/cmdline does not exist (expected on macOS in unit tests; also covers
// the OS error path on Linux if the file is absent).
func TestParse_FileMissing(t *testing.T) {
	if _, err := os.Open("/proc/cmdline"); err == nil {
		// Running on a real Linux — /proc/cmdline exists; skip this test.
		t.Skip("/proc/cmdline exists; skipping file-missing path test")
	}
	_, err := cmdline.Parse()
	if err == nil {
		t.Fatal("expected error when /proc/cmdline is absent, got nil")
	}
}

func TestParseString_DuplicateParam_LastWins(t *testing.T) {
	// If the kernel cmdline somehow has a duplicate, last value wins.
	input := "gough_token=first gough_token=second gough_primary=https://p.example.com gough_mac=aa:bb:cc:dd:ee:ff"
	p, err := cmdline.ParseString(input)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if p.Token != "second" {
		t.Errorf("expected last token value %q, got %q", "second", p.Token)
	}
}
