//go:build noxdp

package cmd

import (
	"bytes"
	"testing"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

// TestBiomeUpgradeCmd_Deferred tests biome upgrade with deferred response.
func TestBiomeUpgradeCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "upgrade queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "upgrade", "k8s-primary", "--to", "2.0.0"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestBiomeUpgradeCmd_Success tests biome upgrade with 200 response.
func TestBiomeUpgradeCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "upgrade", "k8s-primary", "--to", "2.0.0"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestLXDShowTrustPasswordCmd_Deferred tests lxd show-trust-password with deferred response.
func TestLXDShowTrustPasswordCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "password retrieval queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"lxd", "show-trust-password", "--reason", "maintenance"})
	_ = rootCmd.Execute()
}

// TestLXDShowTrustPasswordCmd_Success tests lxd show-trust-password with 200 response.
func TestLXDShowTrustPasswordCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"password": "secret-password",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"lxd", "show-trust-password", "--reason", "maintenance"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestNodeRejectCmd_Deferred tests node reject with deferred response.
func TestNodeRejectCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "node rejection queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "reject", "node-abc", "--reason", "hardware-issue"})
	_ = rootCmd.Execute()
}

// TestNodeRejectCmd_Success tests node reject with 200 response.
func TestNodeRejectCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "reject", "node-abc", "--reason", "hardware-issue"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestMigrationTriggerCmd_Deferred tests migration trigger with deferred response.
func TestMigrationTriggerCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "migration queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"migration", "trigger", "biome-inst-1"})
	_ = rootCmd.Execute()
}

// TestMigrationTriggerCmd_Success tests migration trigger with 200 response.
func TestMigrationTriggerCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"migration", "trigger", "biome-inst-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestDRFailbackCmd_Deferred tests DR failback with deferred response.
func TestDRFailbackCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "failback queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"dr", "failback", "site-abc"})
	_ = rootCmd.Execute()
}

// TestDRFailbackCmd_Success tests DR failback with 200 response.
func TestDRFailbackCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"dr", "failback", "site-abc"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestNodeDecommissionCmd_Deferred tests node decommission with deferred response.
func TestNodeDecommissionCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "decommission queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "decommission", "node-xyz", "--reason", "retirement"})
	_ = rootCmd.Execute()
}

// TestNodeEvacuateCmd_Deferred tests node evacuate with deferred response.
func TestNodeEvacuateCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "evacuation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "evacuate", "node-xyz"})
	_ = rootCmd.Execute()
}

// TestNodeRekeyCmd_Deferred tests node rekey with deferred response.
func TestNodeRekeyCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "rekey queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "rekey", "node-xyz"})
	_ = rootCmd.Execute()
}

// TestPrimaryReplaceCmd_Deferred tests primary replace with deferred response.
func TestPrimaryReplaceCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "replace queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"primary", "replace", "new-primary-id"})
	_ = rootCmd.Execute()
}

// TestPrimaryRotateCACmd_Deferred tests primary rotate-ca with deferred response.
func TestPrimaryRotateCACmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "rotation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"primary", "rotate-ca"})
	_ = rootCmd.Execute()
}

// TestPrimaryFrontendSwitchCmd_Deferred tests primary frontend-switch with deferred response.
func TestPrimaryFrontendSwitchCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "switch queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"primary", "frontend-switch", "new-frontend"})
	_ = rootCmd.Execute()
}

// TestStorageQuotaRequestCmd_Deferred tests storage quota-request with deferred response.
func TestStorageQuotaRequestCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "quota request queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"storage", "quota-request", "10Gi"})
	_ = rootCmd.Execute()
}

// TestSyncExportCmd_Deferred tests sync export with deferred response.
func TestSyncExportCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "export queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"sync", "export", "--to", "s3://bucket/path"})
	_ = rootCmd.Execute()
}

// TestSyncImportCmd_Deferred tests sync import with deferred response.
func TestSyncImportCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "import queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"sync", "import", "--from", "s3://bucket/path"})
	_ = rootCmd.Execute()
}

// TestWebhookTestCmd_Deferred tests webhook test with deferred response.
func TestWebhookTestCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "webhook test queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"webhook", "test", "webhook-id"})
	_ = rootCmd.Execute()
}

// TestBiomePromoteCmd_Deferred tests biome promote with deferred response.
func TestBiomePromoteCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "promote queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "promote", "test-biome", "v1.0.0"})
	_ = rootCmd.Execute()
}

// TestBiomeRollbackCmd_Deferred tests biome rollback with deferred response.
func TestBiomeRollbackCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "rollback queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "rollback", "test-biome"})
	_ = rootCmd.Execute()
}

// TestBiomeReSignCmd_Deferred tests biome re-sign with deferred response.
func TestBiomeReSignCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "re-sign queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "re-sign", "test-biome"})
	_ = rootCmd.Execute()
}

// TestLXDRotateTrustPasswordCmd_Deferred tests lxd rotate-trust-password with deferred response.
func TestLXDRotateTrustPasswordCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "rotation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"lxd", "rotate-trust-password"})
	_ = rootCmd.Execute()
}

// TestVaultUnsealCmd_Deferred tests vault unseal with deferred response.
func TestVaultUnsealCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "unseal queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"vault", "unseal"})
	_ = rootCmd.Execute()
}

// TestBiomeEligibilityCheckCmd_Success tests biome eligibility-check with 200 response.
func TestBiomeEligibilityCheckCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]interface{}{
		"eligible": true,
		"reasons":  []string{},
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "eligibility-check", "test-biome", "v1.0.0"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterStatusCmd_Success tests cluster status with 200 response.
func TestClusterStatusCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(client.ClusterStatus{
		ClusterID: "test-cluster",
		State:     "healthy",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestCapacityRisksCmd_Success tests capacity risks with 200 response.
func TestCapacityRisksCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]interface{}{
		"risks": []map[string]string{},
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"capacity", "risks"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterNetworkBaselineStatusCmd_Success tests network baseline status with 200.
func TestClusterNetworkBaselineStatusCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"mode": "three-network",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterAdoptCmd_Success tests cluster adopt with 200 response.
func TestClusterAdoptCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "adopt", "existing-cluster"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterAdoptCmd_Deferred tests cluster adopt with deferred response.
func TestClusterAdoptCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "adoption queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "adopt", "existing-cluster"})
	_ = rootCmd.Execute()
}

// TestClusterUpgradeCmd_Success tests cluster upgrade with 200 response.
func TestClusterUpgradeCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "upgrade", "--to", "v2.0.0"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterUpgradeCmd_Deferred tests cluster upgrade with deferred response.
func TestClusterUpgradeCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "upgrade queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "upgrade", "--to", "v2.0.0"})
	_ = rootCmd.Execute()
}

// TestClusterEvacuateCmd_Deferred tests cluster evacuate with deferred response.
func TestClusterEvacuateCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "evacuation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "evacuate"})
	_ = rootCmd.Execute()
}

// TestConfigSetCmd_Success tests config set with 200 response.
func TestConfigSetCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"config", "set", "key", "value"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestDeploymentCancelCmd_Deferred tests deployment cancel with deferred response.
func TestDeploymentCancelCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "cancel queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"deployment", "cancel", "deploy-1"})
	_ = rootCmd.Execute()
}

// TestDiskSmartRecheckCmd_Deferred tests disk smart-recheck with deferred response.
func TestDiskSmartRecheckCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "recheck queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"disk", "smart-recheck", "disk-1"})
	_ = rootCmd.Execute()
}

// TestDoctorCheckCmd_Deferred tests doctor check with deferred response.
func TestDoctorCheckCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "check queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"doctor", "check"})
	_ = rootCmd.Execute()
}

// TestIntegrationsRotateCredentialsCmd_Deferred tests integrations rotate-credentials with deferred.
func TestIntegrationsRotateCredentialsCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "rotation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"integrations", "rotate-credentials", "integration-1"})
	_ = rootCmd.Execute()
}

// TestIntegrationsValidateScopeCmd_Success tests integrations validate-scope with 200.
func TestIntegrationsValidateScopeCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]interface{}{
		"valid": true,
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"integrations", "validate-scope", "integration-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestAuditVerifyCmd_Success tests audit verify with 200 response.
func TestAuditVerifyCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]interface{}{
		"verified": true,
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"audit", "verify", "--since", "2025-01-01T00:00:00Z"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestAuditExportCmd_Success tests audit export with 200 response.
func TestAuditExportCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]interface{}{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"audit", "export", "--output", "/tmp/audit.jsonl"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestCapacityForecastCmd_Success tests capacity forecast with 200 response.
func TestCapacityForecastCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]interface{}{
		"forecasts": []map[string]string{},
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"capacity", "forecast"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterTagVocabularyCmd_Success tests cluster tag-vocabulary with 200.
func TestClusterTagVocabularyCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]interface{}{
		"tags": []string{},
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "tag-vocabulary"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestDeploymentLogsCmd_Success tests deployment logs with 200 response.
func TestDeploymentLogsCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"deployment", "logs", "deploy-1", "--tail", "10"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestDevNodeSimCmd_Success tests dev node-sim with 200 response.
func TestDevNodeSimCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"dev", "node-sim"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestDevSeedCmd_Success tests dev seed with 200 response.
func TestDevSeedCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"dev", "seed", "--count", "5"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestDevResetCmd_Deferred tests dev reset with deferred response.
func TestDevResetCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "reset queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"dev", "reset"})
	_ = rootCmd.Execute()
}

// TestDiskPlanCmd_Success tests disk plan with 200 response.
func TestDiskPlanCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]interface{}{
		"plan": map[string]string{},
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"disk", "plan"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestStorageListCmd_Success tests storage list with 200 response.
func TestStorageListCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"storage", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterNetworkBaselineConfigureCmd_Deferred tests network baseline configure with deferred.
func TestClusterNetworkBaselineConfigureCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "configuration queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "configure", "--mode", "three-network"})
	_ = rootCmd.Execute()
}

// TestClusterNetworkBaselineMigrateCmd_Deferred tests network baseline migrate with deferred.
func TestClusterNetworkBaselineMigrateCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "migration queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "migrate", "--from-mode", "three-network", "--to-mode", "two-network"})
	_ = rootCmd.Execute()
}

// TestClusterIdentityPlaneStatusCmd_Success tests identity plane status with 200.
func TestClusterIdentityPlaneStatusCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"provider": "skauswatch",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "identity-plane", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestClusterRotateJoinerSecretsCmd_Deferred tests rotate joiner secrets with deferred.
func TestClusterRotateJoinerSecretsCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "rotation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "rotate-joiner-secrets"})
	_ = rootCmd.Execute()
}

// TestConfigGetCmd_Success tests config get with 200 response.
func TestConfigGetCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"value": "test-value",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"config", "get", "key"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestConfigListCmd_Success tests config list with 200 response.
func TestConfigListCmd_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"key1": "value1",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"config", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// TestBiomeEligibilityCheckCmd_Deferred tests biome eligibility-check with deferred.
func TestBiomeEligibilityCheckCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "check queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "eligibility-check", "test-biome", "v1.0.0"})
	_ = rootCmd.Execute()
}
