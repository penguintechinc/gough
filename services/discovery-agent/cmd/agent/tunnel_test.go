//go:build noxdp

package main

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"

	workload "github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/discover"
	"github.com/penguintechinc/gough/services/discovery-agent/internal/testutil"
)

// svidServer serves one canned X.509 SVID over a unix socket, emulating the SPIRE agent.
type svidServer struct {
	workload.UnimplementedSpiffeWorkloadAPIServer
	svid testutil.SVID
}

// FetchX509SVID returns the SVID and then holds the stream until the client disconnects.
func (s *svidServer) FetchX509SVID(_ *workload.X509SVIDRequest, stream workload.SpiffeWorkloadAPI_FetchX509SVIDServer) error {
	if err := stream.Send(&workload.X509SVIDResponse{Svids: []*workload.X509SVID{{
		SpiffeId:    "spiffe://penguintech.io/beta/discovery-agent",
		X509Svid:    s.svid.DER,
		X509SvidKey: s.svid.KeyPKCS8,
		Bundle:      s.svid.DER,
	}}}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return nil
}

// startSVIDServer runs svidServer on a short unix socket path and returns the unix:// address.
func startSVIDServer(t *testing.T, svid testutil.SVID) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "spire")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	sock := filepath.Join(dir, "api.sock")

	lis, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := grpc.NewServer()
	workload.RegisterSpiffeWorkloadAPIServer(srv, &svidServer{svid: svid})
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	return "unix://" + sock
}

// TestOpenTunnel covers each failure stage and the tunnel-run hand-off using a fake SPIRE agent.
func TestOpenTunnel(t *testing.T) {
	svid := testutil.NewSVID(t, "spiffe://penguintech.io/beta/discovery-agent")
	validSocket := startSVIDServer(t, svid)

	tests := []struct {
		name     string
		socket   string
		endpoint string
		timeout  time.Duration
		wantErr  string
		wantCtx  error
	}{
		{name: "bad socket prefix", socket: "/no/scheme.sock", endpoint: "127.0.0.1:1", timeout: time.Second, wantErr: "spire client"},
		{name: "workload API absent", socket: "unix://" + filepath.Join(t.TempDir(), "absent.sock"), endpoint: "127.0.0.1:1", timeout: 300 * time.Millisecond, wantErr: "fetch SVID"},
		{name: "svid fetched, tunnel runs until ctx ends", socket: validSocket, endpoint: "127.0.0.1:1", timeout: 300 * time.Millisecond, wantCtx: context.DeadlineExceeded},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), tc.timeout)
			defer cancel()

			resp := discover.Response{NodeID: "node-1", ControlTunnelEndpoint: tc.endpoint}
			err := openTunnel(ctx, resp, tc.socket)

			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("error = %v, want substring %q", err, tc.wantErr)
				}
				return
			}
			if !errors.Is(err, tc.wantCtx) {
				t.Fatalf("error = %v, want %v", err, tc.wantCtx)
			}
		})
	}
}
