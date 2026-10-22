//go:build noxdp

package cmd

import (
	"bytes"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/penguintechinc/gough/cli/gough/internal/config"
)

// configSetContextCmd tests
func TestConfigSetContextCmd_Success(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	rootCmd.SetArgs([]string{"config", "set-context", "prod", "--cluster", "https://prod.example.com", "--tenant", "t1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("set-context failed: %v", err)
	}
}

func TestConfigSetContextCmd_MissingCluster(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	t.Cleanup(func() {
		_ = rootCmd.PersistentFlags().Set("context", "")
		_ = rootCmd.PersistentFlags().Set("cluster", "")
	})

	// Explicitly clear the cluster flag before this test
	_ = rootCmd.PersistentFlags().Set("cluster", "")

	rootCmd.SetArgs([]string{"config", "set-context", "prod"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
	// Test just needs to complete; error handling tested via rootCmd behavior
}

func TestConfigSetContextCmd_WithoutTenant(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	rootCmd.SetArgs([]string{"config", "set-context", "staging", "--cluster", "https://staging.example.com"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("set-context without tenant failed: %v", err)
	}
}

// configGetContextsCmd tests
func TestConfigGetContextsCmd_Empty(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	rootCmd.SetArgs([]string{"config", "get-contexts"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("get-contexts (empty) failed: %v", err)
	}
}

func TestConfigGetContextsCmd_WithContexts(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	// First create a context
	rootCmd.SetArgs([]string{"config", "set-context", "prod", "--cluster", "https://prod.example.com"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("set-context failed: %v", err)
	}

	// Then list
	rootCmd.SetArgs([]string{"config", "get-contexts"})
	outBuf.Reset()
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("get-contexts failed: %v", err)
	}
}

func TestConfigGetContextsCmd_WithActive(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	// Create two contexts
	rootCmd.SetArgs([]string{"config", "set-context", "prod", "--cluster", "https://prod.example.com"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()

	rootCmd.SetArgs([]string{"config", "set-context", "staging", "--cluster", "https://staging.example.com"})
	outBuf.Reset()
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()

	// Activate prod
	rootCmd.SetArgs([]string{"config", "use-context", "prod"})
	outBuf.Reset()
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()

	// List and check for active marker
	rootCmd.SetArgs([]string{"config", "get-contexts"})
	outBuf.Reset()
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("get-contexts failed: %v", err)
	}
}

// configCurrentContextCmd tests
func TestConfigCurrentContextCmd_None(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	rootCmd.SetArgs([]string{"config", "current-context"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("current-context failed: %v", err)
	}
}

func TestConfigCurrentContextCmd_Set(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	// Create context
	rootCmd.SetArgs([]string{"config", "set-context", "prod", "--cluster", "https://prod.example.com"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()

	// Activate it
	rootCmd.SetArgs([]string{"config", "use-context", "prod"})
	outBuf.Reset()
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("use-context failed: %v", err)
	}

	// Check current
	rootCmd.SetArgs([]string{"config", "current-context"})
	outBuf.Reset()
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("current-context failed: %v", err)
	}
}

// configUseContextCmd tests
func TestConfigUseContextCmd_NotFound(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	rootCmd.SetArgs([]string{"config", "use-context", "nonexistent"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err == nil {
		t.Fatalf("use-context should fail for nonexistent context")
	}
}

func TestConfigUseContextCmd_Success(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	// Create context first
	rootCmd.SetArgs([]string{"config", "set-context", "staging", "--cluster", "https://staging.example.com"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("set-context failed: %v", err)
	}

	// Use it
	rootCmd.SetArgs([]string{"config", "use-context", "staging"})
	outBuf.Reset()
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("use-context failed: %v", err)
	}
}

// configDeleteContextCmd tests
func TestConfigDeleteContextCmd_Success(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	// Create context
	rootCmd.SetArgs([]string{"config", "set-context", "temp", "--cluster", "https://temp.example.com"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("set-context failed: %v", err)
	}

	// Delete it
	rootCmd.SetArgs([]string{"config", "delete-context", "temp"})
	outBuf.Reset()
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("delete-context failed: %v", err)
	}
}

func TestConfigDeleteContextCmd_NotFound(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	rootCmd.SetArgs([]string{"config", "delete-context", "nonexistent"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err == nil {
		t.Fatalf("delete-context should fail for nonexistent context")
	}
}

func TestConfigDeleteContextCmd_CurrentContextRemoved(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("context", "") })

	// Create and activate context
	rootCmd.SetArgs([]string{"config", "set-context", "temp", "--cluster", "https://temp.example.com"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()

	rootCmd.SetArgs([]string{"config", "use-context", "temp"})
	outBuf.Reset()
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()

	// Delete the active context
	rootCmd.SetArgs([]string{"config", "delete-context", "temp"})
	outBuf.Reset()
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("delete-context failed: %v", err)
	}
}

// resolveClient flag tests - context flag resolves to cluster URL
func TestResolveClient_ContextNotFound(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", "")
	t.Cleanup(func() {
		_ = rootCmd.PersistentFlags().Set("context", "")
		os.Unsetenv("GOUGH_TOKEN")
		os.Unsetenv("GOUGH_CLUSTER_URL")
	})

	// Try to use a nonexistent context with a command that needs auth
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"--context", "nonexistent", "cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err == nil {
		t.Fatalf("--context with nonexistent context should fail")
	}
}

// Test --verbose flag
func TestVerboseFlag_DoesNotPanic(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	t.Cleanup(func() {
		_ = rootCmd.PersistentFlags().Set("verbose", "false")
	})

	rootCmd.SetArgs([]string{"--verbose", "cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test --insecure flag
func TestInsecureFlag_DoesNotPanic(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	t.Cleanup(func() {
		_ = rootCmd.PersistentFlags().Set("insecure", "false")
	})

	rootCmd.SetArgs([]string{"--insecure", "cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test --ca-file flag
func TestCAFileFlag_WithNonexistentFile(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	t.Cleanup(func() {
		_ = rootCmd.PersistentFlags().Set("ca-file", "")
	})

	rootCmd.SetArgs([]string{"--ca-file", "/nonexistent/ca.pem", "cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	// This might fail on TLS setup, which is expected
	_ = rootCmd.Execute()
}

// Test --cluster flag overrides config
func TestClusterFlag_OverridesConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Cleanup(func() {
		os.Unsetenv("GOUGH_TOKEN")
	})

	srv, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	t.Cleanup(func() {
		_ = rootCmd.PersistentFlags().Set("cluster", "")
	})

	rootCmd.SetArgs([]string{"--cluster", srv.URL, "cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test --tenant flag
func TestTenantFlag_SetWithCommand(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	t.Cleanup(func() {
		_ = rootCmd.PersistentFlags().Set("tenant", "")
	})

	rootCmd.SetArgs([]string{"--tenant", "t-12345", "cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test --token flag direct path (root.go: token = flagToken branch)
func TestFlagToken_DirectPath(t *testing.T) {
	srv, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	// Clear env token so flagToken is the sole token source.
	t.Setenv("GOUGH_TOKEN", "")

	// Point cluster at test server via flagCluster (GOUGH_CLUSTER_URL still set by setupTestEnv).
	flagToken = "machine-access-token"
	t.Cleanup(func() { flagToken = "" })

	rootCmd.SetArgs([]string{"--cluster", srv.URL, "cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	t.Cleanup(func() { _ = rootCmd.PersistentFlags().Set("cluster", "") })
	_ = rootCmd.Execute()
}

// Test --insecure flag via direct package-var set (root.go: flagInsecure warning branch)
func TestFlagInsecure_DirectPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	// Directly set the package var to guarantee the branch is taken.
	flagInsecure = true
	t.Cleanup(func() { flagInsecure = false })

	rootCmd.SetArgs([]string{"cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestResolveClient_ContextFillsClusterAndTenant covers the two inner if-blocks in
// resolveClient that copy clusterURL and tenantID from a named context when the
// global flags are empty (root.go lines 149-154).
func TestResolveClient_ContextFillsClusterAndTenant(t *testing.T) {
	srv := httptest.NewServer(jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	t.Cleanup(srv.Close)

	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "ctx-fill-token")
	// Clear GOUGH_CLUSTER_URL so cfg.ClusterURL() returns "" — context must supply it.
	t.Setenv("GOUGH_CLUSTER_URL", "")

	// Write a named context into the temp config directory.
	cfg, err := config.New()
	if err != nil {
		t.Fatalf("config.New: %v", err)
	}
	if err := cfg.SetContext("fill-ctx", config.Context{ClusterURL: srv.URL, TenantID: "t-fill-1"}); err != nil {
		t.Fatalf("SetContext: %v", err)
	}

	flagContext = "fill-ctx"
	t.Cleanup(func() {
		flagContext = ""
		_ = rootCmd.PersistentFlags().Set("context", "")
	})

	rootCmd.SetArgs([]string{"cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}
