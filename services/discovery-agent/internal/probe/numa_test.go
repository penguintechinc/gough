//go:build noxdp

package probe_test

import (
	"context"
	"os"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
)

const lscpuGolden = `{
  "lscpu": [
    {"field": "Architecture:", "data": "x86_64"},
    {"field": "CPU(s):", "data": "128"},
    {"field": "Vendor ID:", "data": "GenuineIntel"},
    {"field": "Model name:", "data": "Intel(R) Xeon(R) Platinum 8462Y+"},
    {"field": "Socket(s):", "data": "2"},
    {"field": "Core(s) per socket:", "data": "32"},
    {"field": "Thread(s) per core:", "data": "2"},
    {"field": "Flags:", "data": "avx2 avx512f sev_snp"}
  ]
}`

const numactlGolden = `available: 2 nodes (0-1)
node 0 cpus: 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50 51 52 53 54 55 56 57 58 59 60 61 62 63
node 0 size: 65536 MB
node 0 free: 63210 MB
node 1 cpus: 64 65 66 67 68 69 70 71 72 73 74 75 76 77 78 79 80 81 82 83 84 85 86 87 88 89 90 91 92 93 94 95 96 97 98 99 100 101 102 103 104 105 106 107 108 109 110 111 112 113 114 115 116 117 118 119 120 121 122 123 124 125 126 127
node 1 size: 65536 MB
node 1 free: 64100 MB
node distances:
node   0   1
  0:  10  21
  1:  21  10
`

func TestRunNUMA_ParsesLscpu(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lscpu":                {Output: []byte(lscpuGolden)},
			"numactl":              {Output: []byte(numactlGolden)},
			"lstopo-no-graphics":   {Output: nil, Err: os.ErrNotExist},
		},
	}

	result, err := probe.RunNUMA(context.Background(), runner)
	if err != nil {
		t.Fatalf("RunNUMA: %v", err)
	}

	if v := result.Lscpu.Get("CPU(s)"); v != "128" {
		t.Errorf("CPU(s) = %q, want 128", v)
	}
	if v := result.Lscpu.Get("Vendor ID"); v != "GenuineIntel" {
		t.Errorf("Vendor ID = %q, want GenuineIntel", v)
	}
}

func TestRunNUMA_ParsesNumactl(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lscpu":              {Output: []byte(lscpuGolden)},
			"numactl":            {Output: []byte(numactlGolden)},
			"lstopo-no-graphics": {Output: nil, Err: os.ErrNotExist},
		},
	}

	result, err := probe.RunNUMA(context.Background(), runner)
	if err != nil {
		t.Fatalf("RunNUMA: %v", err)
	}

	if result.Numactl.Available != 2 {
		t.Errorf("Numactl.Available = %d, want 2", result.Numactl.Available)
	}
	if len(result.Numactl.NodeCPUs) != 2 {
		t.Errorf("NodeCPUs count = %d, want 2", len(result.Numactl.NodeCPUs))
	}
	if result.Numactl.NodeMem[0] != 65536 {
		t.Errorf("NodeMem[0] = %d, want 65536", result.Numactl.NodeMem[0])
	}
}

func TestLscpuOutput_GetCaseInsensitive(t *testing.T) {
	runner := &exec.MockRunner{
		Responses: map[string]exec.MockResponse{
			"lscpu":              {Output: []byte(lscpuGolden)},
			"numactl":            {Output: nil, Err: os.ErrNotExist},
			"lstopo-no-graphics": {Output: nil, Err: os.ErrNotExist},
		},
	}

	result, err := probe.RunNUMA(context.Background(), runner)
	if err != nil {
		t.Fatalf("RunNUMA: %v", err)
	}

	// Case-insensitive get with colon.
	v := result.Lscpu.Get("Vendor ID:")
	if v != "GenuineIntel" {
		t.Errorf("Get('Vendor ID:') = %q, want GenuineIntel", v)
	}
}
