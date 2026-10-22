//go:build noxdp

package probe

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
)

// LshwNode is a node in the lshw JSON tree. lshw produces a deeply nested
// tree; we model it generically so that callers can walk the hierarchy
// themselves while retaining the fields we explicitly need at the top level.
type LshwNode struct {
	ID          string            `json:"id"`
	Class       string            `json:"class"`
	Description string            `json:"description"`
	Product     string            `json:"product"`
	Vendor      string            `json:"vendor"`
	Version     string            `json:"version"`
	Serial      string            `json:"serial"`
	BusInfo     string            `json:"businfo"`
	Physid      string            `json:"physid"`
	Slot        string            `json:"slot"`
	Units       string            `json:"units"`
	Size        int64             `json:"size"`
	Capacity    int64             `json:"capacity"`
	Width       int               `json:"width"`
	Clock       int64             `json:"clock"`
	Config      map[string]string `json:"configuration"`
	Capabilities map[string]any   `json:"capabilities"`
	Children    []LshwNode        `json:"children"`
}

// LshwOutput is the parsed result of running lshw -json.
type LshwOutput struct {
	Root LshwNode `json:"root"`
	// Raw is the original JSON bytes for forward-compat forwarding.
	Raw json.RawMessage `json:"-"`
}

// RunLshw executes lshw -json and parses the result.
func RunLshw(ctx context.Context, runner exec.Runner) (LshwOutput, error) {
	out, err := runner.Run(ctx, "lshw", "-json", "-quiet")
	if err != nil {
		return LshwOutput{}, fmt.Errorf("lshw: %w", err)
	}

	var root LshwNode
	if err := json.Unmarshal(out, &root); err != nil {
		return LshwOutput{}, fmt.Errorf("lshw parse: %w", err)
	}

	return LshwOutput{Root: root, Raw: out}, nil
}

// Walk calls fn for every node in the lshw tree (depth-first pre-order).
func (o *LshwOutput) Walk(fn func(n *LshwNode)) {
	walkNode(&o.Root, fn)
}

func walkNode(n *LshwNode, fn func(*LshwNode)) {
	fn(n)
	for i := range n.Children {
		walkNode(&n.Children[i], fn)
	}
}
