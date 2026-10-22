//go:build noxdp

package cmd

import (
	"os"
	"testing"
)

// TestClusterUpgrade_Success tests successful cluster upgrade (200 OK).
func TestClusterUpgrade_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "upgrade"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("cluster upgrade success: %v", err)
	}
}

// TestClusterEvacuate_Success tests successful node evacuation (200 OK).
func TestClusterEvacuate_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "evacuate", "node-abc"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("cluster evacuate success: %v", err)
	}
}

// TestLXDRotateTrustPassword_Success tests successful LXD trust password rotation.
func TestLXDRotateTrustPassword_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"lxd", "rotate-trust-password"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("lxd rotate-trust-password success: %v", err)
	}
}

// TestDRFailback_Success tests successful disaster recovery failback.
func TestDRFailback_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"dr", "failback", "site-dr1"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("dr failback success: %v", err)
	}
}

// TestBiomePromote_Success tests successful biome promotion.
func TestBiomePromote_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "promote", "biome-abc", "1.0.0"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("biome promote success: %v", err)
	}
}

// TestBiomeRollback_Success tests successful biome rollback.
func TestBiomeRollback_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "rollback", "biome-abc"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("biome rollback success: %v", err)
	}
}

// TestBiomeReSign_Success tests successful biome re-signing.
func TestBiomeReSign_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "re-sign", "biome-abc", "1.0.0"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("biome re-sign success: %v", err)
	}
}

// TestNodeDecommission_Success tests successful node decommissioning.
func TestNodeDecommission_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "decommission", "node-abc"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("node decommission success: %v", err)
	}
}

// TestNodeEvacuate_Success tests successful node evacuation.
func TestNodeEvacuate_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "evacuate", "node-abc"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("node evacuate success: %v", err)
	}
}

// TestNodeRekey_Success tests successful node rekeying.
func TestNodeRekey_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "rekey", "node-abc", "--reason", "rotation"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("node rekey success: %v", err)
	}
}

// TestClusterRotateJoinerSecrets_Success tests successful joiner secret rotation.
func TestClusterRotateJoinerSecrets_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "rotate-joiner-secrets"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("cluster rotate-joiner-secrets success: %v", err)
	}
}

// TestDRDrill_Success tests successful disaster recovery drill.
func TestDRDrill_Success(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"dr", "drill"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("dr drill success: %v", err)
	}
}

// TestPrimaryReplace_SuccessPath tests successful primary node replacement.
func TestPrimaryReplace_SuccessPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"primary", "replace", "node-abc"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("primary replace success: %v", err)
	}
}

// TestPrimaryFrontendSwitch_SuccessPath tests successful primary frontend mode switch.
func TestPrimaryFrontendSwitch_SuccessPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"primary", "frontend-switch", "--mode", "vip"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("primary frontend-switch success: %v", err)
	}
}

// TestPrimaryRotateCA_SuccessPath tests successful primary CA rotation.
func TestPrimaryRotateCA_SuccessPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"primary", "rotate-ca"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("primary rotate-ca success: %v", err)
	}
}

// TestPrimaryForceRecover_SuccessPath tests successful primary force recovery.
func TestPrimaryForceRecover_SuccessPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"primary", "force-recover", "--surviving-node", "node-x", "--reason", "test"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("primary force-recover success: %v", err)
	}
}

// TestSyncExport_SuccessPath tests successful sync export.
func TestSyncExport_SuccessPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"sync", "export", "/mnt/usb"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("sync export success: %v", err)
	}
}

// TestSyncImport_SuccessPath tests successful sync import.
func TestSyncImport_SuccessPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"sync", "import", "/mnt/usb"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("sync import success: %v", err)
	}
}

// TestIntegrationsRotate_SuccessPath tests successful integration credential rotation.
func TestIntegrationsRotate_SuccessPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"integrations", "rotate-credentials", "waddleai"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("integrations rotate-credentials success: %v", err)
	}
}

// TestIntegrationsValidate_SuccessPath tests successful integration scope validation.
func TestIntegrationsValidate_SuccessPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"integrations", "validate-scope", "waddleai"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("integrations validate-scope success: %v", err)
	}
}

// TestNodeDeploy_SuccessPath tests successful node deployment.
func TestNodeDeploy_SuccessPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"node", "deploy", "node-abc"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("node deploy success: %v", err)
	}
}

// TestClusterAdopt_SuccessPath tests successful cluster adoption.
func TestClusterAdopt_SuccessPath(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "adopt", "k8s"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("cluster adopt success: %v", err)
	}
}

// TestClusterStatus_SuccessPath tests successful cluster status retrieval.
func TestClusterStatus_SuccessPath(t *testing.T) {
	status := map[string]interface{}{
		"cluster_id":     "test-cluster",
		"state":          "running",
		"node_count":     3,
		"ready_nodes":    3,
		"biome_instances": 5,
		"version":        "1.0.0",
		"vault_sealed":   false,
		"spire_healthy":  true,
		"db_healthy":     true,
		"nats_healthy":   true,
		"k8s_healthy":    true,
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(status)))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "status"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("cluster status success: %v", err)
	}
}

// TestBiomeNewCmd_ViaCobraCommand tests biome new command via cobra.
func TestBiomeNewCmd_ViaCobraCommand(t *testing.T) {
	dir := t.TempDir()
	orig, _ := os.Getwd()
	if err := os.Chdir(dir); err != nil {
		t.Fatalf("chdir: %v", err)
	}
	defer func() { _ = os.Chdir(orig) }()

	// biome new doesn't need API client, just creates local files
	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("GOUGH_CLUSTER_URL", "http://localhost:1") // won't be used
	rootCmd.SetArgs([]string{"biome", "new", "test-biome-cobra"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("biome new: %v", err)
	}
}

// TestConfigList_WithData tests config list with non-empty data.
func TestConfigList_WithData(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	// First set a config value so the list is non-empty
	rootCmd.SetArgs([]string{"config", "set", "cluster.url", "https://example.com"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("config set: %v", err)
	}

	// Now list — should show the non-empty branch
	rootCmd.SetArgs([]string{"config", "list"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("config list: %v", err)
	}
}

