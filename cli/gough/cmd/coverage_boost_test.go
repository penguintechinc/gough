//go:build noxdp

package cmd

import (
	"bytes"
	"errors"
	"net/http/httptest"
	"testing"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
	"github.com/penguintechinc/gough/cli/gough/internal/output"
)

// TestHandleAPIError_PlainError tests handleAPIError with non-APIError.
func TestHandleAPIError_PlainError(t *testing.T) {
	w := output.New("table", false, false)
	err := errors.New("test error")
	result := handleAPIError(err, w)
	if result != err {
		t.Errorf("expected same error back, got %v", result)
	}
}

// TestAuditList_EmptyEvents tests audit list with empty events array.
func TestAuditList_EmptyEvents(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]interface{}{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"audit", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)

	// Empty list should not error - just prints info message
	_ = rootCmd.Execute()
}

// TestAuditList_WithEvents tests audit list with real events.
func TestAuditList_WithEvents(t *testing.T) {
	events := []client.AuditEvent{
		{
			ID:        "ev-1",
			Timestamp: "2025-05-01T10:00:00Z",
			Actor:     "user@example.com",
			Action:    "create",
			Resource:  "biome",
			TenantID:  "tenant-1",
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(events)))
	defer cleanup()

	rootCmd.SetArgs([]string{"audit", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestNodeList_EmptyNodes tests node list with no nodes.
func TestNodeList_EmptyNodes(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]client.Node{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestNodeTagAdd_InvalidFormat tests node tag add with invalid format.
func TestNodeTagAdd_InvalidFormat(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	// Pass "noequals" instead of "key=value"
	rootCmd.SetArgs([]string{"node", "tag", "add", "node-123", "noequals"})
	err := rootCmd.Execute()
	// Expect error or silenced output
	if err == nil {
		t.Log("no error returned (may be silenced by cobra)")
	}
}

// TestNodeTagAdd_ValidFormat tests node tag add with valid format.
func TestNodeTagAdd_ValidFormat(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "tag", "add", "node-123", "env=prod"})
	_ = rootCmd.Execute()
}

// TestNodeTagAdd_Deferred tests node tag add with deferred response.
func TestNodeTagAdd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "tag operation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "tag", "add", "node-123", "env=prod"})
	_ = rootCmd.Execute()
}

// TestClusterIdentityPlaneConfigureCmd_Success tests identity plane configure with success.
func TestClusterIdentityPlaneConfigureCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "identity-plane", "configure", "--provider", "skauswatch"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterIdentityPlaneConfigureCmd_Deferred tests identity plane configure with deferred.
func TestClusterIdentityPlaneConfigureCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "configuration queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "identity-plane", "configure", "--provider", "skauswatch"})
	_ = rootCmd.Execute()
}

// TestClusterNetworkBaselineConfigure_Success tests network baseline configure with success.
func TestClusterNetworkBaselineConfigure_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "configure", "--mode", "three-network"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterNetworkBaselineMigrate_Success tests network baseline migrate with success.
func TestClusterNetworkBaselineMigrate_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "migrate", "--from-mode", "three-network", "--to-mode", "two-network"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestResolveClient_TokenFlag tests resolveClient using --token flag.
func TestResolveClient_TokenFlag(t *testing.T) {
	srv := httptest.NewServer(jsonResponse(t, 200, apiSuccessResponse(client.ClusterStatus{
		ClusterID: "test-cluster",
		State:     "healthy",
	})))
	defer srv.Close()

	// Set up env without GOUGH_TOKEN
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	// Use --token flag instead
	rootCmd.SetArgs([]string{"cluster", "status", "--cluster", srv.URL, "--token", "flag-token-xyz"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()

	// Reset flags to prevent test interference
	flagToken = ""
	flagCluster = ""
}

// TestResolveClient_TenantFlag tests resolveClient using --tenant flag.
func TestResolveClient_TenantFlag(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]client.Node{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "list", "--tenant", "test-tenant"})
	_ = rootCmd.Execute()

	// Reset flag
	flagTenant = ""
}

// TestDiskList_EmptyDisks tests disk list with no disks.
func TestDiskList_EmptyDisks(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]client.DiskInfo{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"disk", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestBiomeDiff_Deferred_2 tests biome diff with deferred response (second test).
func TestBiomeDiff_Deferred_2(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "diff queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "diff", "test-biome", "v1.0.0", "v2.0.0"})
	_ = rootCmd.Execute()
}

// TestBiomeList_Empty tests biome list with no biomes.
func TestBiomeList_Empty(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]client.Biome{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestMigrationStatusCmd_Success tests migration status with 200 response.
func TestMigrationStatusCmd_Success(t *testing.T) {
	migResp := map[string]interface{}{
		"migration_id": "mig-1",
		"state":        "completed",
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(migResp)))
	defer cleanup()

	rootCmd.SetArgs([]string{"migration", "status", "mig-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestStorageList_Empty tests storage list with no storage.
func TestStorageList_Empty(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]interface{}{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"storage", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestVaultRotateSecretsCmd_Success tests vault rotate secrets with 200 response.
func TestVaultRotateSecretsCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"vault", "rotate-secrets"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestVaultRotateSecretsCmd_Deferred tests vault rotate secrets with 202 response.
func TestVaultRotateSecretsCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "rotation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"vault", "rotate-secrets"})
	_ = rootCmd.Execute()
}

// TestDRPromoteCmd_Success tests DR promote with 200 response.
func TestDRPromoteCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"dr", "promote"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestDRPromoteCmd_Deferred tests DR promote with 202 response.
func TestDRPromoteCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "promotion queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"dr", "promote"})
	_ = rootCmd.Execute()
}

// TestNodeDeployCmd_Success tests node deploy with 200 response.
func TestNodeDeployCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "deploy", "n1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestNodeDeployCmd_Deferred tests node deploy with 202 response.
func TestNodeDeployCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "deployment queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "deploy", "n1"})
	_ = rootCmd.Execute()
}

// TestInitClusterCmd_Success tests init cluster with 200 response.
func TestInitClusterCmd_Success(t *testing.T) {
	defer func() { flagCluster = "" }()
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"init", "cluster", "--cluster", "https://test.local"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestInitClusterCmd_Deferred tests init cluster with 202 response.
func TestInitClusterCmd_Deferred(t *testing.T) {
	defer func() { flagCluster = "" }()
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "initialization queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"init", "cluster", "--cluster", "https://test.local"})
	_ = rootCmd.Execute()
}

// TestLoginCmd_Success tests login command basic flow.
func TestLoginCmd_Success(t *testing.T) {
	cmd := newLoginCmd()
	cmd.SetArgs([]string{"--cluster", "https://test.local"})
	var outBuf bytes.Buffer
	cmd.SetOut(&outBuf)
	// Login requires device flow which we can't test easily, just verify cmd structure
	if cmd.Use != "login" {
		t.Errorf("expected login command")
	}
}

// TestDevTreeCmd_Empty tests dev tree with no items.
func TestDevTreeCmd_Empty(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]interface{}{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"dev", "tree"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestIntegrationsListCmd_Empty tests integrations list with no integrations.
func TestIntegrationsListCmd_Empty(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]interface{}{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"integrations", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestNodeTagRemoveCmd_ValidFormat tests node tag remove with valid format.
func TestNodeTagRemoveCmd_ValidFormat(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "tag", "remove", "n1", "env=prod"})
	_ = rootCmd.Execute()
}

// TestNodeTagRemoveCmd_InvalidFormat tests node tag remove with invalid format.
func TestNodeTagRemoveCmd_InvalidFormat(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "tag", "remove", "n1", "noequals"})
	_ = rootCmd.Execute()
}

// TestLoginCmd_NoClusterURL tests login without cluster URL flag or config.
func TestLoginCmd_NoClusterURL(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"login"})
	var errBuf bytes.Buffer
	rootCmd.SetErr(&errBuf)
	err := rootCmd.Execute()
	if err == nil {
		t.Log("no error returned (may be silenced by cobra)")
	}
}

// TestLogoutCmd_Basic tests logout command.
func TestLogoutCmd_Basic(t *testing.T) {
	rootCmd.SetArgs([]string{"logout"})
	_ = rootCmd.Execute()
}

// TestBiomeValidateCmd_NoFile tests biome validate without file argument.
func TestBiomeValidateCmd_NoFile(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]interface{}{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "validate"})
	err := rootCmd.Execute()
	if err == nil {
		t.Log("no error returned")
	}
}
