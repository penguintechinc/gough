//go:build noxdp

package spire_test

import (
	"context"
	"testing"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/spire"
)

func TestNewClient_RequiresUnixPrefix(t *testing.T) {
	_, err := spire.NewClient("/var/run/spire-agent.sock")
	if err == nil {
		t.Error("expected error for path without unix:// prefix")
	}
	if err != nil && len(err.Error()) == 0 {
		t.Error("error message must not be empty")
	}
}

func TestNewClient_EmptyUsesDefault(t *testing.T) {
	c, err := spire.NewClient("")
	if err != nil {
		t.Fatalf("NewClient(''): %v", err)
	}
	if c == nil {
		t.Error("expected non-nil Client")
	}
}

func TestNewClient_ValidSocket(t *testing.T) {
	c, err := spire.NewClient("unix:///run/spire-agent/public/api.sock")
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	if c == nil {
		t.Error("expected non-nil Client")
	}
}

func TestNewClient_TCPSocket(t *testing.T) {
	// tcp:// is also a valid SPIFFE Workload API address.
	c, err := spire.NewClient("unix:///tmp/spire-test.sock")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if c == nil {
		t.Error("expected non-nil Client")
	}
}

func TestFetchSVID_NoSocketReturnsError(t *testing.T) {
	c, err := spire.NewClient("unix:///tmp/definitely-does-not-exist-gough-test.sock")
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	// Fetching SVID should fail because the socket doesn't exist.
	_, err = c.FetchSVID(context.Background(), "penguintech.io")
	if err == nil {
		t.Error("expected error when socket is absent")
	}
}

func TestFetchSVID_InvalidTrustDomain(t *testing.T) {
	c, err := spire.NewClient("unix:///tmp/definitely-does-not-exist-gough-test2.sock")
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	// Should fail on socket, not trust domain parse (socket absent).
	_, err = c.FetchSVID(context.Background(), "penguintech.io")
	if err == nil {
		t.Error("expected error (socket absent)")
	}
}
