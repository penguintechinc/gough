//go:build noxdp

package tunnel

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/penguintechinc/gough/services/discovery-agent/internal/grpcgen/gough"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/tags"
)

// ProbeRunner is the function signature for running hardware probes.
// It is a field on AgentServer so it can be replaced in tests.
type ProbeRunner func(ctx context.Context) (tags.HardwareInput, error)

// AgentServer implements pb.DiscoveryServer on the discovery-agent side.
// Only Probe is fully implemented; other RPCs are delegated to
// UnimplementedDiscoveryServer.
type AgentServer struct {
	pb.UnimplementedDiscoveryServer
	// RunProbes is called by Probe to collect hardware inventory.
	RunProbes ProbeRunner
	// NodeID is the UUID assigned by the api-manager after registration.
	NodeID string
}

// Probe runs the full hardware probe pipeline and returns the inventory
// serialised as JSON in ProbeResponse.ProbeResult.
func (s *AgentServer) Probe(ctx context.Context, req *pb.ProbeRequest) (*pb.ProbeResponse, error) {
	if s.RunProbes == nil {
		return nil, status.Error(codes.Internal, "probe runner not configured")
	}

	slog.Info("Probe RPC received", "params", req.GetProbeParams())

	hw, err := s.RunProbes(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "probe pipeline: %v", err)
	}

	result, err := probeResultJSON(hw)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "marshal probe result: %v", err)
	}

	probeID := fmt.Sprintf("%s:%s", s.NodeID, uuid.NewString())

	slog.Info("Probe RPC complete", "probe_id", probeID)
	return &pb.ProbeResponse{
		ProbeResult: result,
		ProbeId:     probeID,
	}, nil
}

// probeResult is the JSON-serialisable subset of HardwareInput returned by Probe.
type probeResult struct {
	DMI          interface{} `json:"dmi"`
	NICs         interface{} `json:"nics"`
	HardwareTags []string    `json:"hardware_tags"`
	LshwRaw      interface{} `json:"lshw,omitempty"`
	LsblkRaw     interface{} `json:"lsblk,omitempty"`
}

func probeResultJSON(hw tags.HardwareInput) (string, error) {
	hwTags := tags.Discover(hw)

	r := probeResult{
		DMI:          hw.DMI,
		NICs:         hw.NICs,
		HardwareTags: hwTags,
	}
	if hw.Lshw.Raw != nil {
		r.LshwRaw = json.RawMessage(hw.Lshw.Raw)
	}
	if hw.Lsblk.Raw != nil {
		r.LsblkRaw = json.RawMessage(hw.Lsblk.Raw)
	}

	b, err := json.Marshal(r)
	if err != nil {
		return "", err
	}
	return string(b), nil
}
