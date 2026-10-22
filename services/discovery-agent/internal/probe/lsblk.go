//go:build noxdp

package probe

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
)

// BlockDevice represents a single device or partition from lsblk output.
type BlockDevice struct {
	Name       string        `json:"name"`
	KName      string        `json:"kname"`
	Type       string        `json:"type"`       // disk, part, rom, …
	Size       string        `json:"size"`       // human readable e.g. "931.5G"
	SizeBytes  int64         `json:"size-bytes"` // bytes (--bytes flag not used; parse later)
	Rota       bool          `json:"rota"`       // true = rotational (HDD)
	Tran       string        `json:"tran"`       // sata, nvme, usb, sas, …
	PHYSecSize int           `json:"phy-sec"`
	LogSecSize int           `json:"log-sec"`
	Model      string        `json:"model"`
	Serial     string        `json:"serial"`
	WWN        string        `json:"wwn"`
	Vendor     string        `json:"vendor"`
	Rev        string        `json:"rev"`
	Mountpoint string        `json:"mountpoint"`
	Fstype     string        `json:"fstype"`
	Hotplug    bool          `json:"hotplug"`
	Children   []BlockDevice `json:"children"`
}

// LsblkOutput is the parsed result of lsblk --json -O.
type LsblkOutput struct {
	BlockDevices []BlockDevice `json:"blockdevices"`
	// Raw is forwarded to the POST body verbatim.
	Raw json.RawMessage `json:"-"`
}

// RunLsblk executes lsblk --json -O and parses the result.
func RunLsblk(ctx context.Context, runner exec.Runner) (LsblkOutput, error) {
	out, err := runner.Run(ctx, "lsblk", "--json", "-O")
	if err != nil {
		return LsblkOutput{}, fmt.Errorf("lsblk: %w", err)
	}

	var result LsblkOutput
	if err := json.Unmarshal(out, &result); err != nil {
		return LsblkOutput{}, fmt.Errorf("lsblk parse: %w", err)
	}
	result.Raw = out
	return result, nil
}

// TopLevelDisks returns only top-level block devices whose type is "disk".
func (o *LsblkOutput) TopLevelDisks() []BlockDevice {
	var disks []BlockDevice
	for _, bd := range o.BlockDevices {
		if bd.Type == "disk" {
			disks = append(disks, bd)
		}
	}
	return disks
}
