//go:build noxdp

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/penguintechinc/gough/cli/gough/internal/auth"
	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

// setupTestEnv points the CLI at a test server with a fake token.
func setupTestEnv(t *testing.T, handler http.HandlerFunc) (*httptest.Server, func()) {
	t.Helper()
	srv := httptest.NewServer(handler)

	// Use GOUGH_TOKEN to bypass keychain.
	t.Setenv("GOUGH_TOKEN", "test-token-abc123xyz")

	// Point config at test server.
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)

	return srv, func() {
		srv.Close()
		os.Unsetenv("GOUGH_TOKEN")
		os.Unsetenv("GOUGH_CLUSTER_URL")
	}
}

func jsonResponse(t *testing.T, status int, data interface{}) http.HandlerFunc {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(data)
	}
}

func apiSuccessResponse(data interface{}) client.APIResponse {
	raw, _ := json.Marshal(data)
	return client.APIResponse{Status: "success", Data: raw}
}

func TestVersionCmd(t *testing.T) {
	cmd := newVersionCmd()
	buf := &bytes.Buffer{}
	cmd.SetOut(buf)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("version cmd: %v", err)
	}
}

func TestNodeListCmd_Empty(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]client.Node{})))
	defer cleanup()

	cmd := rootCmd
	cmd.SetArgs([]string{"node", "list"})
	var outBuf bytes.Buffer
	cmd.SetOut(&outBuf)

	// Execute directly — no error for empty list.
	_ = cmd.Execute()
}

func TestNodeListCmd_WithNodes(t *testing.T) {
	nodes := []client.Node{
		{ID: "n1", Hostname: "srv01", State: "ready", CPUCount: 8, MemoryMB: 32768},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(nodes)))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "list", "-o", "json"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

func TestClusterStatusCmd(t *testing.T) {
	status := client.ClusterStatus{
		ClusterID:  "cluster-abc",
		State:      "healthy",
		NodeCount:  3,
		ReadyNodes: 3,
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(status)))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "status"})
	_ = rootCmd.Execute()
}

func TestBiomeListCmd(t *testing.T) {
	biomes := []client.Biome{
		{Name: "k8s-primary", Version: "1.0.0", Kind: "infrastructure"},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(biomes)))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "list"})
	_ = rootCmd.Execute()
}

func TestDRDrillCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "drill queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"dr", "drill"})
	_ = rootCmd.Execute()
}

func TestAuditListCmd(t *testing.T) {
	events := []client.AuditEvent{
		{ID: "e1", Timestamp: "2026-01-01T00:00:00Z", Actor: "user@example.com",
			Action: "node.deploy", Resource: "node/n1"},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(events)))
	defer cleanup()

	rootCmd.SetArgs([]string{"audit", "list"})
	_ = rootCmd.Execute()
}

func TestCapacityForecastCmd(t *testing.T) {
	fc := client.CapacityForecast{
		HorizonDays: 7,
		Nodes: []client.NodeForecast{
			{NodeID: "n1", Hostname: "srv01", BreachRisk: "low"},
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(fc)))
	defer cleanup()

	rootCmd.SetArgs([]string{"capacity", "forecast", "--horizon-days", "7"})
	_ = rootCmd.Execute()
}

func TestMigrationPolicyShowCmd(t *testing.T) {
	policy := client.MigrationPolicy{
		MinHealthyNodes:         3,
		MaxConcurrentMigrations: 1,
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(policy)))
	defer cleanup()

	rootCmd.SetArgs([]string{"migration", "policy", "show"})
	_ = rootCmd.Execute()
}

func TestIntegrationsStatusCmd(t *testing.T) {
	integrations := []client.Integration{
		{Name: "squawk", Status: "healthy", Version: "2.0.0"},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(integrations)))
	defer cleanup()

	rootCmd.SetArgs([]string{"integrations", "status"})
	_ = rootCmd.Execute()
}

func TestConfigListCmd(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"config", "list"})
	_ = rootCmd.Execute()
}

func TestBiomeNewCmd_ScaffoldCreatesFiles(t *testing.T) {
	dir := t.TempDir()
	original, _ := os.Getwd()
	_ = os.Chdir(dir)
	defer func() { _ = os.Chdir(original) }()

	if err := scaffoldBiome("test-biome"); err != nil {
		t.Fatalf("scaffoldBiome error: %v", err)
	}

	for _, f := range []string{
		"test-biome/biome.yaml",
		"test-biome/cloud-init/user-data.yaml",
		"test-biome/lxd-profile.yaml",
		"test-biome/tests/smoke.sh",
	} {
		if _, err := os.Stat(f); os.IsNotExist(err) {
			t.Errorf("scaffold missing file: %s", f)
		}
	}

	// Verify biome.yaml contains expected fields.
	raw, _ := os.ReadFile("test-biome/biome.yaml")
	content := string(raw)
	for _, field := range []string{"name: test-biome", "workload_type:", "readiness_probe:"} {
		if !strings.Contains(content, field) {
			t.Errorf("biome.yaml missing field %q", field)
		}
	}
}

func TestTokenHygieneNeverInLogs(t *testing.T) {
	// Verify MaskToken never exposes raw token.
	rawToken := "super-secret-token-1234"
	ts := &auth.TokenSet{AccessToken: rawToken}
	masked := ts.MaskToken()
	if strings.Contains(masked, "super-secret-token") {
		t.Errorf("MaskToken leaked raw token: %q", masked)
	}
	if !strings.HasPrefix(masked, "tok_****") {
		t.Errorf("MaskToken format incorrect: %q", masked)
	}
}

func TestExitCodesFromAPIErrors(t *testing.T) {
	tests := []struct {
		httpStatus int
		wantCode   int
	}{
		{429, 5},
		{401, 3},
		{422, 6},
		{503, 9},
	}
	for _, tt := range tests {
		httpStatus := tt.httpStatus
		wantCode := tt.wantCode
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(httpStatus)
			_ = json.NewEncoder(w).Encode(map[string]string{"error": "test error"})
		}))

		// Use direct URL without /api/v1 prefix (client.New adds it).
		c := client.New(srv.URL, "tok")
		_, err := c.ListNodes(context.Background(), client.NodeListParams{})
		if err == nil {
			t.Errorf("HTTP %d: expected error", httpStatus)
		} else if apiErr, ok := err.(*client.APIError); !ok {
			t.Errorf("HTTP %d: expected *APIError, got %T: %v", httpStatus, err, err)
		} else if apiErr.ExitCode() != wantCode {
			t.Errorf("HTTP %d: exit code = %d; want %d", httpStatus, apiErr.ExitCode(), wantCode)
		}
		srv.Close()
	}
}
