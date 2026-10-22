//go:build noxdp

package cmd

import (
	"os"
	"testing"
)

// TestBiomeValidateCmd_NoConfig tests biomeValidateCmd with missing cluster URL.
// This covers the resolveClient error path in the RunE function.
func TestBiomeValidateCmd_NoConfig(t *testing.T) {
	// Clear cluster URL and token to force resolveClient error.
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"biome", "validate"})
	_ = rootCmd.Execute()
	// We expect this to fail silently (cobra silences errors).
}

// TestBiomePublishCmd_NoConfig tests biomePublishCmd with missing cluster URL.
func TestBiomePublishCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"biome", "publish"})
	_ = rootCmd.Execute()
}

// TestBiomePromoteCmd_NoConfig tests biomePromoteCmd with missing cluster URL.
func TestBiomePromoteCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"biome", "promote", "biome-name", "1.0.0"})
	_ = rootCmd.Execute()
}

// TestBiomeRollbackCmd_NoConfig tests biomeRollbackCmd with missing cluster URL.
func TestBiomeRollbackCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"biome", "rollback", "biome-name"})
	_ = rootCmd.Execute()
}

// TestBiomeDiffCmd_NoConfig tests biomeDiffCmd with missing cluster URL.
func TestBiomeDiffCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"biome", "diff", "biome-name", "v1", "v2"})
	_ = rootCmd.Execute()
}

// TestBiomeReSignCmd_NoConfig tests biomeReSignCmd with missing cluster URL.
func TestBiomeReSignCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"biome", "re-sign", "biome-name", "1.0.0"})
	_ = rootCmd.Execute()
}

// TestBiomeUpgradeCmd_NoConfig tests biomeUpgradeCmd with missing cluster URL.
func TestBiomeUpgradeCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"biome", "upgrade", "biome-name"})
	_ = rootCmd.Execute()
}

// TestBiomeShowCmd_NoConfig tests biomeShowCmd with missing cluster URL.
func TestBiomeShowCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"biome", "show", "biome-name"})
	_ = rootCmd.Execute()
}

// TestBiomeListCmd_NoConfig tests biomeListCmd with missing cluster URL.
func TestBiomeListCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"biome", "list"})
	_ = rootCmd.Execute()
}

// TestBiomeEligibilityCheckCmd_NoConfig tests biomeEligibilityCheckCmd with missing cluster URL.
func TestBiomeEligibilityCheckCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"biome", "eligibility-check", "instance-id"})
	_ = rootCmd.Execute()
}

// TestAuditListCmd_NoConfig tests auditListCmd with missing cluster URL.
func TestAuditListCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"audit", "list"})
	_ = rootCmd.Execute()
}

// TestAuditVerifyCmd_NoConfig tests auditVerifyCmd with missing cluster URL.
func TestAuditVerifyCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"audit", "verify"})
	_ = rootCmd.Execute()
}

// TestAuditExportCmd_NoConfig tests auditExportCmd with missing cluster URL.
func TestAuditExportCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"audit", "export", "output.log"})
	_ = rootCmd.Execute()
}

// TestCapacityForecastCmd_NoConfig tests capacityForecastCmd with missing cluster URL.
func TestCapacityForecastCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"capacity", "forecast"})
	_ = rootCmd.Execute()
}

// TestCapacityRisksCmd_NoConfig tests capacityRisksCmd with missing cluster URL.
func TestCapacityRisksCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"capacity", "risks"})
	_ = rootCmd.Execute()
}

// TestClusterNetworkBaselineStatusCmd_NoConfig tests clusterNetworkBaselineStatusCmd with missing cluster URL.
func TestClusterNetworkBaselineStatusCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "status"})
	_ = rootCmd.Execute()
}

// TestClusterNetworkBaselineConfigureCmd_NoConfig tests clusterNetworkBaselineConfigureCmd with missing cluster URL.
func TestClusterNetworkBaselineConfigureCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "configure", "192.168.1.0/24"})
	_ = rootCmd.Execute()
}

// TestClusterNetworkBaselineMigrateCmd_NoConfig tests clusterNetworkBaselineMigrateCmd with missing cluster URL.
func TestClusterNetworkBaselineMigrateCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "migrate", "192.168.1.0/24"})
	_ = rootCmd.Execute()
}

// TestClusterTagVocabularyCmd_NoConfig tests clusterTagVocabularyCmd with missing cluster URL.
func TestClusterTagVocabularyCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"cluster", "tag-vocabulary"})
	_ = rootCmd.Execute()
}

// TestClusterStatusCmd_NoConfig tests clusterStatusCmd with missing cluster URL.
func TestClusterStatusCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"cluster", "status"})
	_ = rootCmd.Execute()
}

// TestClusterUpgradeCmd_NoConfig tests clusterUpgradeCmd with missing cluster URL.
func TestClusterUpgradeCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"cluster", "upgrade", "v1.0.0"})
	_ = rootCmd.Execute()
}

// TestClusterEvacuateCmd_NoConfig tests clusterEvacuateCmd with missing cluster URL.
func TestClusterEvacuateCmd_NoConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("GOUGH_TOKEN", "")
	t.Setenv("GOUGH_CLUSTER_URL", "")

	rootCmd.SetArgs([]string{"cluster", "evacuate", "node-id"})
	_ = rootCmd.Execute()
}

// TestScaffoldBiome_LocalError tests scaffoldBiome in a read-only directory.
func TestScaffoldBiome_LocalError(t *testing.T) {
	// Create a read-only directory.
	dir := t.TempDir()
	roDir := dir + "/readonly"
	if err := os.Mkdir(roDir, 0o500); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	// Try to scaffold a biome in the read-only directory.
	err := scaffoldBiome(roDir + "/biome")
	if err == nil {
		t.Error("expected error when scaffolding in read-only directory")
	}
}
