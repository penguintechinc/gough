//go:build noxdp

// Package tunnel manages the persistent bidirectional gRPC-mTLS control
// tunnel from the discovery-agent to the api-manager.
package tunnel

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"log/slog"
	"sync/atomic"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/metrics"
	pb "github.com/penguintechinc/gough/services/discovery-agent/internal/grpcgen/gough"
)

const (
	heartbeatInterval  = 15 * time.Second
	backoffInitial     = 5 * time.Second
	backoffMax         = 60 * time.Second
	backoffMultiplier  = 2.0
)

// TunnelConfig holds parameters for creating a control tunnel.
type TunnelConfig struct {
	// Endpoint is "host:8443" of the api-manager gRPC endpoint.
	Endpoint string
	// TLSConfig is an mTLS config populated from the SPIRE SVID bundle.
	TLSConfig *tls.Config
	// NodeID is reported in every heartbeat.
	NodeID string
	// SPIFFEID is the agent's own SPIFFE ID (informational).
	SPIFFEID string
}

// Tunnel manages the long-lived gRPC stream to api-manager.
type Tunnel struct {
	cfg     TunnelConfig
	seq     atomic.Int64
	conn    *grpc.ClientConn
	client  pb.DiscoveryClient
	stream  grpc.BidiStreamingClient[pb.DiscoveryEventStreamRequest, pb.DiscoveryEventStreamResponse]
}

// New creates a Tunnel but does not connect yet.
func New(cfg TunnelConfig) *Tunnel {
	return &Tunnel{cfg: cfg}
}

// Run opens the tunnel and blocks, reconnecting with exponential backoff on
// disconnect. ctx cancellation is the only way to stop.
func (t *Tunnel) Run(ctx context.Context) error {
	backoff := backoffInitial

	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		slog.Info("tunnel: connecting", "endpoint", t.cfg.Endpoint)
		if err := t.connect(ctx); err != nil {
			metrics.TunnelDropTotal.Inc()
			slog.Warn("tunnel: connect failed", "err", err, "retry_in", backoff)
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(backoff):
			}
			backoff = min(time.Duration(float64(backoff)*backoffMultiplier), backoffMax)
			continue
		}
		backoff = backoffInitial // reset on successful connect

		slog.Info("tunnel: connected", "endpoint", t.cfg.Endpoint, "spiffe_id", t.cfg.SPIFFEID)
		if err := t.runStream(ctx); err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			metrics.TunnelDropTotal.Inc()
			slog.Warn("tunnel: stream ended", "err", err, "retry_in", backoff)
		}

		t.close()

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
		backoff = min(time.Duration(float64(backoff)*backoffMultiplier), backoffMax)
	}
}

func (t *Tunnel) connect(ctx context.Context) error {
	creds := credentials.NewTLS(t.cfg.TLSConfig)
	conn, err := grpc.NewClient(
		t.cfg.Endpoint,
		grpc.WithTransportCredentials(creds),
	)
	if err != nil {
		return fmt.Errorf("grpc.NewClient: %w", err)
	}
	t.conn = conn
	t.client = pb.NewDiscoveryClient(conn)

	stream, err := t.client.EventStream(ctx)
	if err != nil {
		conn.Close()
		t.conn = nil
		return fmt.Errorf("EventStream: %w", err)
	}
	t.stream = stream
	return nil
}

func (t *Tunnel) runStream(ctx context.Context) error {
	// Start sender goroutine for heartbeats.
	sendErrCh := make(chan error, 1)
	go func() {
		sendErrCh <- t.sendLoop(ctx)
	}()

	// Receive loop.
	for {
		resp, err := t.stream.Recv()
		if err != nil {
			if err == io.EOF {
				return fmt.Errorf("stream EOF")
			}
			return fmt.Errorf("recv: %w", err)
		}
		slog.Debug("tunnel: received event", "type", resp.EventType, "payload", resp.EventPayload)
		// Future: dispatch resp.EventType to handler registry.
	}
}

func (t *Tunnel) sendLoop(ctx context.Context) error {
	ticker := time.NewTicker(heartbeatInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			seq := t.seq.Add(1)
			req := &pb.DiscoveryEventStreamRequest{
				EventFilter: "heartbeat",
				EventTypes:  []string{"heartbeat"},
			}
			// Encode sequence ID into the filter string (api-manager uses it for resume).
			req.EventFilter = fmt.Sprintf("heartbeat:seq=%d:node=%s", seq, t.cfg.NodeID)
			if err := t.stream.Send(req); err != nil {
				return fmt.Errorf("send heartbeat: %w", err)
			}
		}
	}
}

func (t *Tunnel) close() {
	if t.stream != nil {
		_ = t.stream.CloseSend()
		t.stream = nil
	}
	if t.conn != nil {
		t.conn.Close()
		t.conn = nil
	}
}

// SendEvent sends a single non-heartbeat event to the api-manager.
func (t *Tunnel) SendEvent(ctx context.Context, eventType, payload string) error {
	if t.stream == nil {
		return fmt.Errorf("tunnel: not connected")
	}
	seq := t.seq.Add(1)
	return t.stream.Send(&pb.DiscoveryEventStreamRequest{
		EventFilter: fmt.Sprintf("%s:seq=%d", eventType, seq),
		EventTypes:  []string{eventType, payload},
	})
}

func min(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}
