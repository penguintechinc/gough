//go:build noxdp

package cmd

import (
	"bytes"
	"testing"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

// These tests target specific commands and edge cases not covered
// in existing test files to improve code coverage.

// Biome Eligibility Check with more realistic data
func TestBiomeEligibilityCheck_WithEligibilityData(t *testing.T) {
	data := map[string]interface{}{
		"migration_eligible": true,
		"upgrade_eligible":   true,
		"migration_reasons":  []string{},
		"upgrade_reasons":    []string{},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(data)))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "eligibility-check", "biome-instance-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("biome eligibility-check: %v", err)
	}
}

// Node Show with full node data
func TestNodeShow_WithFullNodeData(t *testing.T) {
	node := client.Node{
		ID:               "node-xyz",
		Hostname:         "srv-prod-01",
		State:            "ready",
		DMIUUID:          "550e8400-e29b-41d4-a716-446655440000",
		MgmtMAC:          "aa:bb:cc:dd:ee:ff",
		CPUCount:         16,
		MemoryMB:         65536,
		Arch:             "x86_64",
		TenantID:         "tenant-abc",
		BiomeInstanceIDs: []string{"biome-1", "biome-2"},
		CreatedAt:        "2026-01-01T00:00:00Z",
		UpdatedAt:        "2026-01-02T00:00:00Z",
		Tags: map[string]string{
			"env":  "prod",
			"zone": "us-east-1",
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(node)))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "show", "node-xyz"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("node show: %v", err)
	}
}

// Node Tags with many tags
func TestNodeTags_WithManyTags(t *testing.T) {
	tags := map[string]string{
		"env":    "prod",
		"gpu":    "nvidia",
		"cpu":    "high-performance",
		"memory": "large",
		"zone":   "us-east-1",
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(tags)))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "tags", "node-abc"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("node tags with many tags: %v", err)
	}
}

// Storage Quota List with empty result
func TestStorageQuotaList_Empty(t *testing.T) {
	quotas := []client.StorageQuota{}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(quotas)))
	defer cleanup()

	rootCmd.SetArgs([]string{"storage", "list-quotas"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Storage Quota List with single item
func TestStorageQuotaList_SingleItem(t *testing.T) {
	quotas := []client.StorageQuota{
		{
			ID:           "quota-1",
			TenantID:     "tenant-1",
			ResourceType: "block_storage",
			LimitValue:   100.0,
			UsedValue:    75.5,
			Unit:         "GiB",
			UpdatedAt:    "2026-01-01T12:00:00Z",
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(quotas)))
	defer cleanup()

	rootCmd.SetArgs([]string{"storage", "list-quotas"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("storage list-quotas single: %v", err)
	}
}

// Migration Trigger with target node
func TestMigrationTrigger_WithTargetNode(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "migration_started",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"migration", "trigger", "biome-instance-1", "--target", "node-xy"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("migration trigger with target: %v", err)
	}
}

// Migration Trigger with ignore-lock and reason
func TestMigrationTrigger_WithIgnoreLockAndReason(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "migration_started",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"migration", "trigger", "biome-instance-1", "--ignore-lock", "--reason", "maintenance"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("migration trigger with ignore-lock: %v", err)
	}
}

// Audit Export with since flag
func TestAuditExport_WithSinceOnly(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "export_initiated",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"audit", "export", "--since", "2026-01-01T00:00:00Z", "--output", "/tmp/audit.jsonl"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("audit export with since: %v", err)
	}
}

// DR Promote with source-unreachable flag
func TestDRPromote_WithSourceUnreachableFlag(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "initiated",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"dr", "promote", "site-dr1", "--reason", "primary_failure", "--source-unreachable"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("dr promote with source-unreachable: %v", err)
	}
}

// Integrations Configure with multiple options
func TestIntegrationsConfigure_WithMultipleOptions(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "configured",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"integrations", "configure", "squawk",
		"--opt", "endpoint=https://squawk.example.com",
		"--opt", "zone=gough.local",
	})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("integrations configure multiple opts: %v", err)
	}
}

// Dev Seed with multiple biomes
func TestDevSeed_WithMultipleBiomes(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "seeding",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"dev", "seed", "--biomes", "k8s-primary,nest-agent,tobogganing"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("dev seed multiple biomes: %v", err)
	}
}

// Cluster Network Baseline Migrate with squawk provider
func TestClusterNetworkBaselineMigrate_WithSquawk(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "migration_started",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "migrate", "mgmt", "--to", "squawk"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("cluster network-baseline migrate to squawk: %v", err)
	}
}

// LXD Show Trust Password with audit reason
func TestLXDShowTrustPassword_WithAuditReason(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"password": "secret-trust-password-xyz",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"lxd", "show-trust-password", "--reason", "cluster-maintenance"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("lxd show-trust-password with reason: %v", err)
	}
}

// Node Reject with optional reason
func TestNodeReject_WithReason(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "rejected",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "reject", "node-new", "--reason", "hardware_issue"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("node reject with reason: %v", err)
	}
}

// Biome Upgrade to specific version
func TestBiomeUpgrade_ToVersion(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "upgrade_initiated",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "upgrade", "k8s-primary", "--to", "1.28.5"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("biome upgrade to version: %v", err)
	}
}

// Deployment Cancel with valid deployment id
func TestDeploymentCancel_WithID(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "cancelled",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"deployment", "cancel", "deploy-abc123"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("deployment cancel: %v", err)
	}
}

// TestBiomeValidateCmd_WithTestFlag tests biomeValidateCmd with --test flag.
func TestBiomeValidateCmd_WithTestFlag(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "passed",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "validate", "--test"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterStatusCmd_Ready tests cluster status command.
func TestClusterStatusCmd_Ready(t *testing.T) {
	status := map[string]interface{}{
		"state":       "ready",
		"nodes_ready": 5,
		"nodes_total": 5,
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(status)))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterAdoptCmd_NewNode tests cluster adopt command.
func TestClusterAdoptCmd_NewNode(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "adoption_initiated",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "adopt", "node-new-01"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestMigrationPolicyShowCmd tests migration policy-show command.
func TestMigrationPolicyShowCmd_Show(t *testing.T) {
	policy := map[string]interface{}{
		"max_concurrent":  2,
		"timeout_seconds": 300,
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(policy)))
	defer cleanup()

	rootCmd.SetArgs([]string{"migration", "policy-show"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestDRDrillCmd_Site tests dr drill command.
func TestDRDrillCmd_Site(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "drill_initiated",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"dr", "drill", "site-dr1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}
