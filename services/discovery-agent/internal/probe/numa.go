//go:build noxdp

package probe

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
)

// LscpuEntry is one key/value pair from lscpu --json output.
type LscpuEntry struct {
	Field    string `json:"field"`
	Data     string `json:"data"`
	Children []LscpuEntry `json:"children,omitempty"`
}

// LscpuOutput holds parsed lscpu data.
type LscpuOutput struct {
	Lscpu []LscpuEntry `json:"lscpu"`
	// Lookup provides quick access by field name.
	m map[string]string
}

// Get returns the value for a given lscpu field (case-insensitive, colon stripped).
func (l *LscpuOutput) Get(field string) string {
	if l.m == nil {
		l.m = make(map[string]string)
		for _, e := range l.Lscpu {
			key := strings.ToLower(strings.TrimRight(e.Field, ":"))
			l.m[key] = e.Data
		}
	}
	return l.m[strings.ToLower(strings.TrimRight(field, ":"))]
}

// NumactlOutput holds the parsed output of numactl --hardware.
type NumactlOutput struct {
	Available int             `json:"available"`
	NodeCPUs  map[int][]int   `json:"node_cpus"`
	NodeMem   map[int]int64   `json:"node_mem_mb"`
	Raw       string          `json:"raw"`
}

// LstopoOutput holds raw lstopo-no-graphics --of json output.
type LstopoOutput struct {
	Raw json.RawMessage `json:"raw"`
}

// NUMAProbeResult bundles all NUMA-related data.
type NUMAProbeResult struct {
	Lscpu   LscpuOutput   `json:"lscpu"`
	Numactl NumactlOutput `json:"numactl"`
	Lstopo  LstopoOutput  `json:"lstopo"`
}

// RunNUMA runs lscpu, numactl --hardware, and lstopo-no-graphics.
func RunNUMA(ctx context.Context, runner exec.Runner) (NUMAProbeResult, error) {
	var result NUMAProbeResult

	// lscpu --json
	lscpuOut, err := runner.Run(ctx, "lscpu", "--json")
	if err != nil {
		return result, fmt.Errorf("lscpu: %w", err)
	}
	if err := json.Unmarshal(lscpuOut, &result.Lscpu); err != nil {
		return result, fmt.Errorf("lscpu parse: %w", err)
	}

	// numactl --hardware (text output; parse manually)
	numactlOut, err := runner.Run(ctx, "numactl", "--hardware")
	if err == nil {
		result.Numactl = parseNumactl(string(numactlOut))
	}

	// lstopo-no-graphics --of json (best-effort)
	lsTopoOut, err := runner.Run(ctx, "lstopo-no-graphics", "--of", "json")
	if err == nil && len(lsTopoOut) > 0 {
		result.Lstopo.Raw = lsTopoOut
	}

	return result, nil
}

// parseNumactl parses the text output of numactl --hardware.
func parseNumactl(text string) NumactlOutput {
	out := NumactlOutput{
		NodeCPUs: make(map[int][]int),
		NodeMem:  make(map[int]int64),
		Raw:      text,
	}

	scanner := bufio.NewScanner(strings.NewReader(text))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())

		// available: 2 nodes (0-1)
		if strings.HasPrefix(line, "available:") {
			fmt.Sscanf(line, "available: %d", &out.Available)
			continue
		}

		// node 0 cpus: 0 1 2 3 4 5 ...
		var nodeID int
		if strings.Contains(line, "cpus:") {
			if n, _ := fmt.Sscanf(line, "node %d cpus:", &nodeID); n == 1 {
				rest := line[strings.Index(line, "cpus:")+5:]
				fields := strings.Fields(strings.TrimSpace(rest))
				var cpus []int
				for _, f := range fields {
					var cpu int
					if _, err := fmt.Sscanf(f, "%d", &cpu); err == nil {
						cpus = append(cpus, cpu)
					}
				}
				out.NodeCPUs[nodeID] = cpus
			}
			continue
		}

		// node 0 size: 32153 MB
		if strings.Contains(line, "size:") && strings.Contains(line, "MB") {
			var mb int64
			if n, _ := fmt.Sscanf(line, "node %d size: %d MB", &nodeID, &mb); n == 2 {
				out.NodeMem[nodeID] = mb
			}
		}
	}
	return out
}
