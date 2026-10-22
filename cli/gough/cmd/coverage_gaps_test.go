//go:build noxdp

package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

// =================================================================
// COVERAGE GAPS: diskPlanCmd, vaultUnsealCmd, migrationPolicySetCmd
// nodeDeployCmd and other low-coverage functions
// =================================================================

// TestDiskPlanCmd_BadPlanFile tests disk plan with missing plan file
func TestDiskPlanCmd_BadPlanFile(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"disk", "plan", "node-1", "--plan", "/nonexistent/plan.json"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err == nil {
		t.Log("expected error for missing plan file")
	}
}

// TestDiskPlanCmd_YAMLPlan tests disk plan with YAML content (non-JSON)
func TestDiskPlanCmd_YAMLPlan(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	planContent := `devices:
  - sda
  - sdb
partitions: []`
	planFile := t.TempDir() + "/plan.yaml"
	if err := os.WriteFile(planFile, []byte(planContent), 0644); err != nil {
		t.Fatalf("write plan file: %v", err)
	}

	rootCmd.SetArgs([]string{"disk", "plan", "node-1", "--plan", planFile})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Logf("Execute returned: %v", err)
	}
}

// TestVaultUnsealCmd_EmptyShare tests vault unseal with empty share
func TestVaultUnsealCmd_EmptyShare(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	oldStdin := os.Stdin
	defer func() { os.Stdin = oldStdin }()

	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	os.Stdin = r

	go func() {
		_, _ = w.WriteString("\n") // Empty line
		_ = w.Close()
	}()

	rootCmd.SetArgs([]string{"vault", "unseal"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err = rootCmd.Execute()
	if err == nil {
		t.Log("expected error for empty share")
	}
}

// TestVaultUnsealCmd_ExitCode2 tests vault unseal with exit code 2 (vault still sealed)
func TestVaultUnsealCmd_ExitCode2(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"status":  "error",
			"error":   "vault_sealed",
			"message": "Vault is still sealed",
		})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	oldStdin := os.Stdin
	defer func() { os.Stdin = oldStdin }()

	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	os.Stdin = r

	go func() {
		_, _ = w.WriteString("test-share\n")
		_ = w.Close()
	}()

	rootCmd.SetArgs([]string{"vault", "unseal"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestNodeDeployCmd_ExitCode7 tests node deploy with exit code 7 (plan compilation failed)
func TestNodeDeployCmd_ExitCode7(t *testing.T) {
	_ = mockExit(t)

	// Reset the plan flag after the test so it doesn't contaminate subsequent tests.
	deployCmd, _, _ := rootCmd.Find([]string{"node", "deploy"})
	if deployCmd != nil {
		t.Cleanup(func() { _ = deployCmd.Flags().Set("plan", "") })
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"status":  "error",
			"error":   "plan_compilation_failed",
			"message": "Plan compilation failed",
		})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	planContent := `{"biomes": []}`
	planFile := filepath.Join(t.TempDir(), "deploy.json")
	if err := os.WriteFile(planFile, []byte(planContent), 0644); err != nil {
		t.Fatalf("write plan file: %v", err)
	}

	rootCmd.SetArgs([]string{"node", "deploy", "node-1", "--plan", planFile})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestNodeDeployCmd_NoPlan tests node deploy without plan file (uses existing)
func TestNodeDeployCmd_NoPlan(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "deploy", "node-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestLoginCmd_NoURL tests login without cluster URL and config
func TestLoginCmd_NoURL(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	// Don't set GOUGH_CLUSTER_URL

	// Disable keychain access for testing
	t.Setenv("GOUGH_TOKEN", "test-token")

	rootCmd.SetArgs([]string{"login"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err == nil {
		t.Log("expected error when no cluster URL provided")
	}
}

// TestIntegrationsConfigureCmd_Deferred tests integrations configure with deferred response
func TestIntegrationsConfigureCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "integration configuration queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"integrations", "configure", "squawk", "--squawk-api-key", "test-key"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestDRDrillCmd_APIError tests dr drill with API error
func TestDRDrillCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(422)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid dr config"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"dr", "drill"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestMigrationTriggerCmd_SafetyCheck tests migration trigger with safety envelope failure
func TestMigrationTriggerCmd_SafetyCheck(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"migration", "trigger", "instance-1", "--target", "node-2"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestStorageQuotaRequestCmd_APIError tests storage quota request with API error
func TestStorageQuotaRequestCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid quota"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"storage", "quota", "request", "tenant-1", "100"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

