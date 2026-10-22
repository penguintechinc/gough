//go:build noxdp

// Package tunnel manages the persistent bidirectional gRPC-mTLS control
// tunnel from the discovery-agent to the api-manager.
package tunnel

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sync/atomic"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	pb "github.com/penguintechinc/gough/services/discovery-agent/internal/grpcgen/gough"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/metrics"
)

// Timing knobs. They are variables so tests can shorten them; production never reassigns them.
var (
	heartbeatInterval = 15 * time.Second
	backoffInitial    = 5 * time.Second
	backoffMax        = 60 * time.Second
	backoffMultiplier = 2.0
)

// streamClient is the subset of the bidi stream the tunnel uses.
type streamClient = grpc.BidiStreamingClient[pb.DiscoveryEventStreamRequest, pb.DiscoveryEventStreamResponse]

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
	cfg    TunnelConfig
	seq    atomic.Int64
	conn   *grpc.ClientConn
	client pb.DiscoveryClient
	stream streamClient
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
			backoff = nextBackoff(backoff)
			continue
		}
		backoff = backoffInitial // reset on successful connect

		slog.Info("tunnel: connected", "endpoint", t.cfg.Endpoint, "spiffe_id", t.cfg.SPIFFEID)
		if err := t.runStream(ctx); err != nil {
			if ctx.Err() != nil {
				t.close()
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
		backoff = nextBackoff(backoff)
	}
}

// nextBackoff returns the next reconnect delay: current * multiplier, capped at backoffMax.
func nextBackoff(cur time.Duration) time.Duration {
	return min(time.Duration(float64(cur)*backoffMultiplier), backoffMax)
}

// connect dials the endpoint and opens the event stream. On failure the
// connection is released and t.conn/t.stream stay nil.
func (t *Tunnel) connect(ctx context.Context) error {
	creds := credentials.NewTLS(t.cfg.TLSConfig)
	conn, err := grpc.NewClient(
		t.cfg.Endpoint,
		grpc.WithTransportCredentials(creds),
	)
	if err != nil {
		return fmt.Errorf("grpc.NewClient: %w", err)
	}
	client := pb.NewDiscoveryClient(conn)

	stream, err := client.EventStream(ctx)
	if err != nil {
		_ = conn.Close()
		return fmt.Errorf("EventStream: %w", err)
	}
	t.conn = conn
	t.client = client
	t.stream = stream
	return nil
}

// runStream receives events until the stream ends. A heartbeat sender runs for
// the lifetime of this call and is cancelled and joined before return, so it
// never touches a stream after the caller tears it down.
func (t *Tunnel) runStream(ctx context.Context) error {
	stream := t.stream

	sendCtx, cancelSend := context.WithCancel(ctx)
	// The sender's exit error (ctx.Err on teardown, or a failed heartbeat send)
	// is not actionable here: the Recv loop reports the stream failure itself.
	sendDone := make(chan struct{})
	go func() {
		defer close(sendDone)
		_ = sendLoop(sendCtx, stream, t.cfg.NodeID, &t.seq)
	}()
	defer func() {
		cancelSend()
		<-sendDone
	}()

	for {
		resp, err := stream.Recv()
		if err != nil {
			if errors.Is(err, io.EOF) {
				return fmt.Errorf("stream EOF")
			}
			return fmt.Errorf("recv: %w", err)
		}
		slog.Debug("tunnel: received event", "type", resp.EventType, "payload", resp.EventPayload)
		// Future: dispatch resp.EventType to handler registry.
	}
}

// sendLoop emits a heartbeat on every heartbeatInterval tick until ctx is done
// or a send fails. The stream and sequence counter are passed in explicitly so
// the loop never reads shared Tunnel fields.
func sendLoop(ctx context.Context, stream streamClient, nodeID string, seq *atomic.Int64) error {
	ticker := time.NewTicker(heartbeatInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			n := seq.Add(1)
			req := &pb.DiscoveryEventStreamRequest{
				EventFilter: fmt.Sprintf("heartbeat:seq=%d:node=%s", n, nodeID),
				EventTypes:  []string{"heartbeat"},
			}
			if err := stream.Send(req); err != nil {
				return fmt.Errorf("send heartbeat: %w", err)
			}
		}
	}
}

// close releases the stream and connection. Safe to call when already closed.
func (t *Tunnel) close() {
	if t.stream != nil {
		_ = t.stream.CloseSend()
		t.stream = nil
	}
	if t.conn != nil {
		_ = t.conn.Close()
		t.conn = nil
	}
}

// SendEvent sends a single non-heartbeat event to the api-manager.
func (t *Tunnel) SendEvent(_ context.Context, eventType, payload string) error {
	if t.stream == nil {
		return fmt.Errorf("tunnel: not connected")
	}
	n := t.seq.Add(1)
	return t.stream.Send(&pb.DiscoveryEventStreamRequest{
		EventFilter: fmt.Sprintf("%s:seq=%d", eventType, n),
		EventTypes:  []string{eventType, payload},
	})
}

func min(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}
