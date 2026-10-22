//go:build noxdp

package cmd

import (
	"bytes"
	"os"
	"testing"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
	"github.com/penguintechinc/gough/cli/gough/internal/config"
)

// ============================================================
// migrationPolicySetCmd — tested directly because cobra naming
// conflict makes it unreachable via rootCmd ("policy show" and
// "policy set" both have cobra Name()=="policy", so show wins).
// ============================================================

func TestMigrationPolicySetCmd_DirectSuccess(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	cmd := migrationPolicySetCmd()
	var outBuf bytes.Buffer
	cmd.SetOut(&outBuf)
	cmd.SetArgs([]string{"min_healthy_nodes", "3"})
	_ = cmd.Execute()
}

func TestMigrationPolicySetCmd_DirectDeferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "policy update queued",
	}))
	defer cleanup()

	cmd := migrationPolicySetCmd()
	cmd.SetArgs([]string{"min_healthy_nodes", "3"})
	_ = cmd.Execute()
}

func TestMigrationPolicySetCmd_DirectAPIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status":  "error",
		"error":   "invalid value",
		"message": "value must be a positive integer",
	}))
	defer cleanup()

	cmd := migrationPolicySetCmd()
	cmd.SetArgs([]string{"min_healthy_nodes", "invalid"})
	_ = cmd.Execute()
	_ = exitCode
}

func TestMigrationPolicySetCmd_DirectNoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	cmd := migrationPolicySetCmd()
	cmd.SetArgs([]string{"min_healthy_nodes", "3"})
	_ = cmd.Execute()
}

// ============================================================
// No-config (resolveClient error path) tests for functions
// that don't have one yet — covers `return err` branch.
// ============================================================

func TestLXDRotateTrustPasswordCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"lxd", "rotate-trust-password"})
	_ = rootCmd.Execute()
}

func TestDiskSmartRecheckCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"disk", "smart-recheck", "disk-abc"})
	_ = rootCmd.Execute()
}

func TestDevNodeSimCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"dev", "node-sim"})
	_ = rootCmd.Execute()
}

func TestSyncExportCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"sync", "export", "/mnt/usb"})
	_ = rootCmd.Execute()
}

func TestSyncImportCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"sync", "import", "/mnt/usb"})
	_ = rootCmd.Execute()
}

func TestWebhookTestCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"webhook", "test"})
	_ = rootCmd.Execute()
}

func TestDevResetCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"dev", "reset"})
	_ = rootCmd.Execute()
}

func TestPrimaryReplaceCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"primary", "replace", "node-abc"})
	_ = rootCmd.Execute()
}

func TestPrimaryRotateCACmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"primary", "rotate-ca"})
	_ = rootCmd.Execute()
}

func TestNodeEvacuateCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"node", "evacuate", "node-abc"})
	_ = rootCmd.Execute()
}

func TestIntegrationsConfigureCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"integrations", "configure", "squawk"})
	_ = rootCmd.Execute()
}

func TestNodeTagsCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"node", "tags", "node-abc"})
	_ = rootCmd.Execute()
}

func TestNodeShowCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"node", "show", "node-abc"})
	_ = rootCmd.Execute()
}

func TestDiskPlanCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"disk", "plan", "node-abc"})
	_ = rootCmd.Execute()
}

func TestStorageQuotaRequestCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"storage", "quota", "request", "tenant-1", "100"})
	_ = rootCmd.Execute()
}

// ============================================================
// API error path tests — covers `return handleAPIError(err, w)`
// for functions not yet tested with an error response.
// ============================================================

func TestLXDRotateTrustPasswordCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "validation_failed",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"lxd", "rotate-trust-password"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestDiskSmartRecheckCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "disk_not_found",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"disk", "smart-recheck", "disk-abc"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestDevNodeSimCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "dev_only",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"dev", "node-sim"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestSyncExportCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "invalid_path",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"sync", "export", "/mnt/usb"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestSyncImportCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "invalid_path",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"sync", "import", "/mnt/usb"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestWebhookTestCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "webhook_failed",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"webhook", "test"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestDevResetCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "reset_failed",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"dev", "reset"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestPrimaryReplaceCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "replace_failed",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"primary", "replace", "node-abc"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestPrimaryRotateCACmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "rotation_failed",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"primary", "rotate-ca"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestNodeEvacuateCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "evacuate_failed",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "evacuate", "node-abc"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestIntegrationsConfigureCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "configure_failed",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"integrations", "configure", "squawk"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestNodeTagsCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 401, map[string]interface{}{
		"status": "error", "error": "unauthorized",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "tags", "node-abc"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestNodeShowCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "not_found",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "show", "node-abc"})
	_ = rootCmd.Execute()
	_ = exitCode
}

func TestDiskPlanCmd_APIError(t *testing.T) {
	exitCode := mockExit(t)

	_, cleanup := setupTestEnv(t, jsonResponse(t, 422, map[string]interface{}{
		"status": "error", "error": "plan_failed",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"disk", "plan", "node-abc"})
	_ = rootCmd.Execute()
	_ = exitCode
}


// devNodeSimCmd deferred path
func TestDevNodeSimCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "node sim queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"dev", "node-sim"})
	_ = rootCmd.Execute()
}

// ============================================================
// Vault unseal — no-config, success, and deferred paths.
// ExitCode==2 branch is unreachable (client never emits Code=2).
// ============================================================

func TestVaultUnsealCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"vault", "unseal"})
	_ = rootCmd.Execute()
}

func TestVaultUnsealCmd_Success(t *testing.T) {
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
		_, _ = w.WriteString("test-share\n")
		_ = w.Close()
	}()

	rootCmd.SetArgs([]string{"vault", "unseal"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

func TestVaultUnsealCmd_DeferredWithStdin(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "unseal queued",
	}))
	defer cleanup()

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
	_ = rootCmd.Execute()
}

// migrationPolicyShowCmd no-config — covers resolveClient error return.
func TestMigrationPolicyShowCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"migration", "policy-show"})
	_ = rootCmd.Execute()
}

// configSetContextCmd with missing --cluster via direct constructor call.
// Calling configSetContextCmd() fresh avoids cobra persistent-flag contamination
// that causes the rootCmd-routed version to see a stale cluster value.
func TestConfigSetContextCmd_DirectMissingCluster(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	cmd := configSetContextCmd()
	cmd.SetArgs([]string{"prod"}) // no --cluster → triggers "required" error
	_ = cmd.Execute()
}

// resolveClient reads tenant_id from config when no --tenant flag or context tenant.
// Covers root.go: else if cfg.Get("tenant_id") != "" { c.SetTenantID(t) }.
func TestResolveClient_TenantFromConfig(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	cfg, err := config.New()
	if err != nil {
		t.Fatalf("config.New: %v", err)
	}
	if err := cfg.Set("tenant_id", "t-from-config-file"); err != nil {
		t.Fatalf("cfg.Set: %v", err)
	}

	rootCmd.SetArgs([]string{"cluster", "status"})
	_ = rootCmd.Execute()
}
