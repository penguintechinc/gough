//go:build noxdp

package tunnel

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	pb "github.com/penguintechinc/gough/services/discovery-agent/internal/grpcgen/gough"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/testutil"
)

// fakeDiscovery is an in-process api-manager stand-in over mTLS-style TLS.
type fakeDiscovery struct {
	pb.UnimplementedDiscoveryServer

	// closeImmediately makes EventStream return at once, simulating a dropped stream.
	closeImmediately bool

	mu        sync.Mutex
	conns     int
	received  []*pb.DiscoveryEventStreamRequest
	connected chan struct{}
}

func newFakeDiscovery(closeImmediately bool) *fakeDiscovery {
	return &fakeDiscovery{closeImmediately: closeImmediately, connected: make(chan struct{}, 16)}
}

// EventStream records the connection, then either returns or records every request.
func (f *fakeDiscovery) EventStream(stream grpc.BidiStreamingServer[pb.DiscoveryEventStreamRequest, pb.DiscoveryEventStreamResponse]) error {
	f.mu.Lock()
	f.conns++
	f.mu.Unlock()
	select {
	case f.connected <- struct{}{}:
	default: // reconnect tests may outpace the buffer; the count above is authoritative
	}

	if f.closeImmediately {
		return nil
	}
	for {
		req, err := stream.Recv()
		if err != nil {
			return nil
		}
		f.mu.Lock()
		f.received = append(f.received, req)
		f.mu.Unlock()
	}
}

func (f *fakeDiscovery) connCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.conns
}

func (f *fakeDiscovery) requests() []*pb.DiscoveryEventStreamRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]*pb.DiscoveryEventStreamRequest, len(f.received))
	copy(out, f.received)
	return out
}

// startServer runs fake on a loopback TLS listener and returns its address and client TLS config.
func startServer(t *testing.T, fake *fakeDiscovery) (string, *tls.Config) {
	t.Helper()
	cert := testutil.NewSelfSignedCert(t)

	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := grpc.NewServer(grpc.Creds(credentials.NewTLS(&tls.Config{
		Certificates: []tls.Certificate{cert.TLS},
		MinVersion:   tls.VersionTLS12,
	})))
	pb.RegisterDiscoveryServer(srv, fake)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)

	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(cert.CAPEM)
	return lis.Addr().String(), &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
}

// withFastTiming shortens tunnel timers for the duration of a test.
func withFastTiming(t *testing.T) {
	t.Helper()
	hb, bi, bm := heartbeatInterval, backoffInitial, backoffMax
	heartbeatInterval = 10 * time.Millisecond
	backoffInitial = 5 * time.Millisecond
	backoffMax = 20 * time.Millisecond
	t.Cleanup(func() {
		heartbeatInterval, backoffInitial, backoffMax = hb, bi, bm
	})
}

// runAsync starts Run in a goroutine and returns a channel carrying its result.
func runAsync(tn *Tunnel, ctx context.Context) <-chan error {
	done := make(chan error, 1)
	go func() { done <- tn.Run(ctx) }()
	return done
}

// TestRunHeartbeats verifies the tunnel connects, emits sequenced heartbeats, and stops on cancel.
func TestRunHeartbeats(t *testing.T) {
	withFastTiming(t)
	fake := newFakeDiscovery(false)
	addr, tlsCfg := startServer(t, fake)

	tn := New(TunnelConfig{Endpoint: addr, TLSConfig: tlsCfg, NodeID: "node-7", SPIFFEID: "spiffe://penguintech.io/beta/agent"})
	ctx, cancel := context.WithCancel(context.Background())
	done := runAsync(tn, ctx)

	deadline := time.After(10 * time.Second)
	for {
		hbs := 0
		for _, r := range fake.requests() {
			if strings.HasPrefix(r.EventFilter, "heartbeat:seq=") && strings.Contains(r.EventFilter, ":node=node-7") {
				hbs++
			}
		}
		if hbs >= 2 {
			break
		}
		select {
		case <-deadline:
			cancel()
			if err := <-done; err != nil {
				t.Logf("run teardown: %v", err)
			}
			t.Fatalf("saw %d heartbeats, want >=2", hbs)
		case <-time.After(5 * time.Millisecond):
		}
	}

	cancel()
	err := <-done
	if !errors.Is(err, context.Canceled) {
		t.Errorf("Run error = %v, want context.Canceled", err)
	}
	if fake.connCount() != 1 {
		t.Errorf("connections = %d, want 1", fake.connCount())
	}
}

// TestRunReconnectsAfterStreamEnds verifies a dropped stream triggers a reconnect.
func TestRunReconnectsAfterStreamEnds(t *testing.T) {
	withFastTiming(t)
	fake := newFakeDiscovery(true)
	addr, tlsCfg := startServer(t, fake)

	tn := New(TunnelConfig{Endpoint: addr, TLSConfig: tlsCfg, NodeID: "n"})
	ctx, cancel := context.WithCancel(context.Background())
	done := runAsync(tn, ctx)

	deadline := time.After(10 * time.Second)
	for fake.connCount() < 3 {
		select {
		case <-deadline:
			cancel()
			if err := <-done; err != nil {
				t.Logf("run teardown: %v", err)
			}
			t.Fatalf("connections = %d, want >=3 (reconnect loop)", fake.connCount())
		case <-time.After(5 * time.Millisecond):
		}
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Errorf("Run error = %v, want context.Canceled", err)
	}
}

// TestRunConnectFailureBacksOff verifies unreachable endpoints are retried until cancelled.
func TestRunConnectFailureBacksOff(t *testing.T) {
	withFastTiming(t)
	cert := testutil.NewSelfSignedCert(t)
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(cert.CAPEM)

	// Port 1 on loopback is closed: EventStream fails fast with Unavailable.
	tn := New(TunnelConfig{Endpoint: "127.0.0.1:1", TLSConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}, NodeID: "n"})
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()

	err := tn.Run(ctx)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("Run error = %v, want context.DeadlineExceeded", err)
	}
}

// TestRunCancelledBeforeStart returns immediately without dialing.
func TestRunCancelledBeforeStart(t *testing.T) {
	tn := New(TunnelConfig{Endpoint: "127.0.0.1:1", NodeID: "n"})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := tn.Run(ctx); !errors.Is(err, context.Canceled) {
		t.Errorf("Run error = %v, want context.Canceled", err)
	}
}

// TestSendEvent covers the not-connected guard and delivery over a live stream.
func TestSendEvent(t *testing.T) {
	withFastTiming(t)
	heartbeatInterval = time.Hour // keep heartbeats out of the assertion window

	tests := []struct {
		name      string
		connected bool
		eventType string
		payload   string
		wantErr   bool
	}{
		{name: "not connected", connected: false, eventType: "disk", payload: "x", wantErr: true},
		{name: "connected delivers", connected: true, eventType: "disk.failed", payload: "sda"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			fake := newFakeDiscovery(false)
			addr, tlsCfg := startServer(t, fake)
			tn := New(TunnelConfig{Endpoint: addr, TLSConfig: tlsCfg, NodeID: "n"})

			if !tc.connected {
				err := tn.SendEvent(context.Background(), tc.eventType, tc.payload)
				if (err != nil) != tc.wantErr {
					t.Fatalf("SendEvent error = %v, wantErr %v", err, tc.wantErr)
				}
				return
			}

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if err := tn.connect(ctx); err != nil {
				t.Fatalf("connect: %v", err)
			}
			defer tn.close()

			if err := tn.SendEvent(ctx, tc.eventType, tc.payload); err != nil {
				t.Fatalf("SendEvent: %v", err)
			}

			deadline := time.After(5 * time.Second)
			for {
				found := false
				for _, r := range fake.requests() {
					if len(r.EventTypes) == 2 && r.EventTypes[0] == tc.eventType && r.EventTypes[1] == tc.payload {
						found = true
					}
				}
				if found {
					return
				}
				select {
				case <-deadline:
					t.Fatalf("event %s/%s not received", tc.eventType, tc.payload)
				case <-time.After(5 * time.Millisecond):
				}
			}
		})
	}
}

// TestConnectErrors covers a bad TLS endpoint and that failed connects leave no residue.
func TestConnectErrors(t *testing.T) {
	tn := New(TunnelConfig{Endpoint: "127.0.0.1:1", TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12}})
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	if err := tn.connect(ctx); err == nil {
		t.Fatal("connect to closed port: expected error")
	}
	if tn.conn != nil || tn.stream != nil {
		t.Error("failed connect left conn/stream set")
	}
	tn.close() // must be a no-op on a never-connected tunnel
}

// TestNextBackoff verifies exponential growth capped at backoffMax.
func TestNextBackoff(t *testing.T) {
	withFastTiming(t)
	backoffMax = 60 * time.Second

	tests := []struct {
		name string
		cur  time.Duration
		want time.Duration
	}{
		{name: "doubles", cur: 5 * time.Second, want: 10 * time.Second},
		{name: "doubles again", cur: 10 * time.Second, want: 20 * time.Second},
		{name: "caps at max", cur: 40 * time.Second, want: 60 * time.Second},
		{name: "stays at max", cur: 60 * time.Second, want: 60 * time.Second},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := nextBackoff(tc.cur); got != tc.want {
				t.Errorf("nextBackoff(%v) = %v, want %v", tc.cur, got, tc.want)
			}
		})
	}
}

// TestMin covers both branches of the package-level min helper.
func TestMin(t *testing.T) {
	tests := []struct {
		name string
		a, b time.Duration
		want time.Duration
	}{
		{name: "a smaller", a: 1, b: 2, want: 1},
		{name: "b smaller", a: 3, b: 2, want: 2},
		{name: "equal", a: 2, b: 2, want: 2},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := min(tc.a, tc.b); got != tc.want {
				t.Errorf("min(%v,%v) = %v, want %v", tc.a, tc.b, got, tc.want)
			}
		})
	}
}
