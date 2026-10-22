//go:build noxdp

package spire

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	workload "github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/testutil"
)

const agentID = "spiffe://penguintech.io/beta/discovery-agent"

// fakeWorkloadAPI serves canned X.509 SVID responses over a unix socket.
type fakeWorkloadAPI struct {
	workload.UnimplementedSpiffeWorkloadAPIServer
	svids []*workload.X509SVID
	err   error
}

// FetchX509SVID sends the canned SVIDs, then holds the stream open until the client leaves.
func (f *fakeWorkloadAPI) FetchX509SVID(_ *workload.X509SVIDRequest, s workload.SpiffeWorkloadAPI_FetchX509SVIDServer) error {
	if f.err != nil {
		return f.err
	}
	if err := s.Send(&workload.X509SVIDResponse{Svids: f.svids}); err != nil {
		return err
	}
	<-s.Context().Done()
	return nil
}

// startWorkloadAPI listens on a unix socket in a short temp dir (sun_path is length-limited)
// and returns the unix:// address to pass to NewClient.
func startWorkloadAPI(t *testing.T, fake *fakeWorkloadAPI) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "wlapi")
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
	workload.RegisterSpiffeWorkloadAPIServer(srv, fake)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	return "unix://" + sock
}

// TestNewClient checks socket-path validation and the default path.
func TestNewClient(t *testing.T) {
	tests := []struct {
		name    string
		socket  string
		wantErr bool
	}{
		{name: "empty uses default", socket: ""},
		{name: "unix prefix accepted", socket: "unix:///run/spire/api.sock"},
		{name: "missing unix prefix rejected", socket: "/run/spire/api.sock", wantErr: true},
		{name: "tcp scheme rejected", socket: "tcp://127.0.0.1:8080", wantErr: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			c, err := NewClient(tc.socket)
			if (err != nil) != tc.wantErr {
				t.Fatalf("NewClient(%q) error = %v, wantErr %v", tc.socket, err, tc.wantErr)
			}
			if tc.wantErr {
				return
			}
			want := tc.socket
			if want == "" {
				want = defaultSocketPath
			}
			if c.socketPath != want {
				t.Errorf("socketPath = %q, want %q", c.socketPath, want)
			}
		})
	}
}

// TestFetchSVID drives FetchSVID against a fake Workload API for each trust-domain and response shape.
func TestFetchSVID(t *testing.T) {
	good := testutil.NewSVID(t, agentID)

	tests := []struct {
		name        string
		fake        *fakeWorkloadAPI
		trustDomain string
		wantErr     string
		wantID      string
	}{
		{
			name:        "svid with matching trust domain",
			fake:        &fakeWorkloadAPI{svids: []*workload.X509SVID{{SpiffeId: agentID, X509Svid: good.DER, X509SvidKey: good.KeyPKCS8, Bundle: good.DER}}},
			trustDomain: "penguintech.io",
			wantID:      agentID,
		},
		{
			name:        "empty trust domain authorises any peer",
			fake:        &fakeWorkloadAPI{svids: []*workload.X509SVID{{SpiffeId: agentID, X509Svid: good.DER, X509SvidKey: good.KeyPKCS8, Bundle: good.DER}}},
			trustDomain: "",
			wantID:      agentID,
		},
		{
			name:        "invalid trust domain rejected",
			fake:        &fakeWorkloadAPI{svids: []*workload.X509SVID{{SpiffeId: agentID, X509Svid: good.DER, X509SvidKey: good.KeyPKCS8, Bundle: good.DER}}},
			trustDomain: "Bad Domain!",
			wantErr:     "invalid trust domain",
		},
		{
			name:        "no SVIDs returned",
			fake:        &fakeWorkloadAPI{},
			trustDomain: "penguintech.io",
			wantErr:     "no SVIDs",
		},
		{
			name:        "workload API returns error",
			fake:        &fakeWorkloadAPI{err: status.Error(codes.PermissionDenied, "not entitled")},
			trustDomain: "penguintech.io",
			wantErr:     "fetch X509 context",
		},
		{
			name:        "malformed SVID material",
			fake:        &fakeWorkloadAPI{svids: []*workload.X509SVID{{SpiffeId: agentID, X509Svid: []byte("junk"), X509SvidKey: []byte("junk"), Bundle: []byte("junk")}}},
			trustDomain: "penguintech.io",
			wantErr:     "fetch X509 context",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			addr := startWorkloadAPI(t, tc.fake)
			c, err := NewClient(addr)
			if err != nil {
				t.Fatalf("NewClient: %v", err)
			}

			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			bundle, err := c.FetchSVID(ctx, tc.trustDomain)

			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("error = %v, want substring %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("FetchSVID: %v", err)
			}
			if bundle.SPIFFEID != tc.wantID {
				t.Errorf("SPIFFEID = %q, want %q", bundle.SPIFFEID, tc.wantID)
			}
			if bundle.TLSConfig == nil {
				t.Fatal("TLSConfig is nil")
			}
			// MTLSClientConfig presents the SVID through GetClientCertificate, not a static slice.
			if bundle.TLSConfig.GetClientCertificate == nil {
				t.Error("GetClientCertificate is nil; SVID not wired for mTLS")
			}
		})
	}
}

// TestFetchSVIDNoWorkloadAPI asserts that an absent socket fails within the caller's deadline.
func TestFetchSVIDNoWorkloadAPI(t *testing.T) {
	c, err := NewClient("unix://" + filepath.Join(t.TempDir(), "absent.sock"))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	_, err = c.FetchSVID(ctx, "penguintech.io")
	if err == nil {
		t.Fatal("expected error when workload API socket is absent")
	}
}
