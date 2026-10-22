//go:build noxdp

package tunnel

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/penguintechinc/gough/services/discovery-agent/internal/grpcgen/gough"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/probe"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/tags"
)

// TestAgentServerProbe covers unconfigured, failing, and successful probe runs.
func TestAgentServerProbe(t *testing.T) {
	okHW := tags.HardwareInput{DMI: probe.DMIInfo{SysVendor: "Dell", ProductName: "R640"}}

	tests := []struct {
		name     string
		runner   ProbeRunner
		wantCode codes.Code
		wantOK   bool
	}{
		{name: "runner not configured", runner: nil, wantCode: codes.Internal},
		{
			name: "pipeline failure",
			runner: func(context.Context) (tags.HardwareInput, error) {
				return tags.HardwareInput{}, errors.New("lsblk down")
			},
			wantCode: codes.Internal,
		},
		{
			name:   "success returns inventory JSON",
			runner: func(context.Context) (tags.HardwareInput, error) { return okHW, nil },
			wantOK: true,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			s := &AgentServer{RunProbes: tc.runner, NodeID: "node-42"}
			resp, err := s.Probe(context.Background(), &pb.ProbeRequest{ProbeParams: "full"})
			if !tc.wantOK {
				if status.Code(err) != tc.wantCode {
					t.Fatalf("code = %v, want %v (err=%v)", status.Code(err), tc.wantCode, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("Probe: %v", err)
			}
			if !strings.HasPrefix(resp.GetProbeId(), "node-42:") {
				t.Errorf("ProbeId = %q, want node-42: prefix", resp.GetProbeId())
			}
			var m map[string]any
			if err := json.Unmarshal([]byte(resp.GetProbeResult()), &m); err != nil {
				t.Fatalf("ProbeResult is not JSON: %v", err)
			}
			if _, ok := m["dmi"]; !ok {
				t.Errorf("ProbeResult missing dmi: %s", resp.GetProbeResult())
			}
		})
	}
}

// TestProbeResultJSON checks which raw blobs are forwarded and that tags are always present.
func TestProbeResultJSON(t *testing.T) {
	tests := []struct {
		name       string
		hw         tags.HardwareInput
		wantKeys   []string
		absentKeys []string
	}{
		{
			name:       "no raw blobs",
			hw:         tags.HardwareInput{},
			wantKeys:   []string{"dmi", "nics", "hardware_tags"},
			absentKeys: []string{"lshw", "lsblk"},
		},
		{
			name: "lshw and lsblk raw forwarded",
			hw: tags.HardwareInput{
				Lshw:  probe.LshwOutput{Raw: json.RawMessage(`{"class":"system"}`)},
				Lsblk: probe.LsblkOutput{Raw: json.RawMessage(`{"blockdevices":[]}`)},
			},
			wantKeys: []string{"lshw", "lsblk", "hardware_tags"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			out, err := probeResultJSON(tc.hw)
			if err != nil {
				t.Fatalf("probeResultJSON: %v", err)
			}
			var m map[string]json.RawMessage
			if err := json.Unmarshal([]byte(out), &m); err != nil {
				t.Fatalf("not JSON: %v: %s", err, out)
			}
			for _, k := range tc.wantKeys {
				if _, ok := m[k]; !ok {
					t.Errorf("key %q missing from %s", k, out)
				}
			}
			for _, k := range tc.absentKeys {
				if _, ok := m[k]; ok {
					t.Errorf("key %q should be omitted from %s", k, out)
				}
			}
		})
	}
}
