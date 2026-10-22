//go:build noxdp

// Package spire wraps the SPIFFE Workload API to fetch X.509 SVIDs for
// mTLS authentication with the api-manager gRPC tunnel.
package spire

import (
	"context"
	"crypto/tls"
	"fmt"
	"strings"

	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
	"github.com/spiffe/go-spiffe/v2/workloadapi"
)

const defaultSocketPath = "unix:///tmp/spire-agent/public/api.sock"

// SVIDBundle holds the SVID + peer config needed for gRPC mTLS dials.
type SVIDBundle struct {
	// TLSConfig is ready to use as tls.Config for a gRPC credential.
	TLSConfig *tls.Config
	// SPIFFEID is the identity of this node.
	SPIFFEID string
}

// Client fetches SVIDs from the SPIRE Workload API.
type Client struct {
	socketPath string
}

// NewClient creates a Client that contacts the Workload API at socketPath.
// socketPath must have the "unix://" prefix; if empty the default path is used.
func NewClient(socketPath string) (*Client, error) {
	if socketPath == "" {
		socketPath = defaultSocketPath
	}
	if !strings.HasPrefix(socketPath, "unix://") {
		return nil, fmt.Errorf("spire: socket path must have unix:// prefix, got %q", socketPath)
	}
	return &Client{socketPath: socketPath}, nil
}

// FetchSVID retrieves an X.509 SVID from the Workload API and returns a
// TLS config suitable for mTLS gRPC dials.  It authorises connections only
// to peers with an ID under serverTrustDomain (e.g. "penguintech.io").
func (c *Client) FetchSVID(ctx context.Context, serverTrustDomain string) (SVIDBundle, error) {
	wc, err := workloadapi.New(ctx, workloadapi.WithAddr(c.socketPath))
	if err != nil {
		return SVIDBundle{}, fmt.Errorf("spire: new workload client: %w", err)
	}
	defer wc.Close()

	x509Ctx, err := wc.FetchX509Context(ctx)
	if err != nil {
		return SVIDBundle{}, fmt.Errorf("spire: fetch X509 context: %w", err)
	}

	if len(x509Ctx.SVIDs) == 0 {
		return SVIDBundle{}, fmt.Errorf("spire: no SVIDs returned from workload API")
	}

	svid := x509Ctx.SVIDs[0]

	// Build authorizer: only allow peers whose SPIFFE ID is under serverTrustDomain.
	var authorizer tlsconfig.Authorizer
	if serverTrustDomain != "" {
		td, err := spiffeid.TrustDomainFromString(serverTrustDomain)
		if err != nil {
			return SVIDBundle{}, fmt.Errorf("spire: invalid trust domain %q: %w", serverTrustDomain, err)
		}
		authorizer = tlsconfig.AuthorizeMemberOf(td)
	} else {
		authorizer = tlsconfig.AuthorizeAny()
	}

	tlsCfg := tlsconfig.MTLSClientConfig(svid, x509Ctx.Bundles, authorizer)

	return SVIDBundle{
		TLSConfig: tlsCfg,
		SPIFFEID:  svid.ID.String(),
	}, nil
}
