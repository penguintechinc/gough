//go:build noxdp && !linux

// Stub implementations for non-Linux platforms (build/test on macOS).
// The discovery-agent is deployed on Linux only; these stubs exist solely
// so that cross-platform CI builds and unit tests compile without errors.
package serial

import "os"

const syscallNoctty = 0

// setSerial is a no-op on non-Linux platforms.
func setSerial(_ *os.File, _ int) error { return nil }
