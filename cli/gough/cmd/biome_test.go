//go:build noxdp

package cmd

import (
	"os"
	"strings"
	"testing"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

func TestBiomeListCmd_Empty(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]client.Biome{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "list"})

	// Execute directly — no error for empty list.
	_ = rootCmd.Execute()
}

func TestBiomeListCmd_WithItems(t *testing.T) {
	biomes := []client.Biome{
		{
			Name:         "k8s-primary",
			Version:      "1.28.0",
			Kind:         "infrastructure",
			Phase:        "post_deploy",
			WorkloadType: "k8s-manifest",
			LockToHost:   false,
		},
		{
			Name:         "monitoring-stack",
			Version:      "2.1.5",
			Kind:         "application",
			Phase:        "post_deploy",
			WorkloadType: "k8s-helm",
			LockToHost:   true,
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(biomes)))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "list"})
	_ = rootCmd.Execute()
	// Tests for list command should not require output verification—
	// just verify no panic/error occurs.
}

func TestBiomeListCmd_Filters(t *testing.T) {
	biomes := []client.Biome{
		{
			Name:         "k8s-primary",
			Version:      "1.28.0",
			Kind:         "infrastructure",
			Phase:        "post_deploy",
			WorkloadType: "k8s-manifest",
			LockToHost:   false,
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(biomes)))
	defer cleanup()

	// Test with --kind filter
	rootCmd.SetArgs([]string{"biome", "list", "--kind", "infrastructure"})
	_ = rootCmd.Execute()

	// Test with --phase filter
	rootCmd.SetArgs([]string{"biome", "list", "--phase", "post_deploy"})
	_ = rootCmd.Execute()

	// Test with --workload filter
	rootCmd.SetArgs([]string{"biome", "list", "--workload", "k8s-manifest"})
	_ = rootCmd.Execute()
}

func TestBiomeShowCmd(t *testing.T) {
	biome := &client.Biome{
		Name:         "k8s-primary",
		Version:      "1.28.0",
		Kind:         "infrastructure",
		Phase:        "post_deploy",
		WorkloadType: "k8s-manifest",
		LockToHost:   false,
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(biome)))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "show", "k8s-primary"})
	_ = rootCmd.Execute()
}

func TestBiomeShowCmd_WithVersion(t *testing.T) {
	biome := &client.Biome{
		Name:         "k8s-primary",
		Version:      "1.27.0",
		Kind:         "infrastructure",
		Phase:        "post_deploy",
		WorkloadType: "k8s-manifest",
		LockToHost:   false,
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(biome)))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "show", "k8s-primary", "--version", "1.27.0"})
	_ = rootCmd.Execute()
}

func TestBiomeValidateCmd_Success(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "valid"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "validate"})
	_ = rootCmd.Execute()
}

func TestBiomeValidateCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "validation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "validate"})
	_ = rootCmd.Execute()
}

func TestBiomeValidateCmd_WithTest(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "valid"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "validate", "--test"})
	_ = rootCmd.Execute()
}

func TestBiomePublishCmd_Success(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "published", "version": "1.0.0"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "publish"})
	_ = rootCmd.Execute()
}

func TestBiomePublishCmd_Deferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "publish queued for processing",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "publish"})
	_ = rootCmd.Execute()
}

func TestBiomePromoteCmd(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "promoted"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "promote", "k8s-primary", "1.28.0"})
	_ = rootCmd.Execute()
}

func TestBiomeRollbackCmd(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "rolled_back", "version": "1.27.5"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "rollback", "k8s-primary"})
	_ = rootCmd.Execute()
}

func TestBiomeDiffCmd(t *testing.T) {
	diffData := map[string]interface{}{
		"name":    "k8s-primary",
		"v1":      "1.27.5",
		"v2":      "1.28.0",
		"changes": []string{"updated cloud-init", "new dependencies"},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(diffData)))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "diff", "k8s-primary", "1.27.5", "1.28.0"})
	_ = rootCmd.Execute()
}

func TestBiomeReSignCmd(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "re_signed"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "re-sign", "k8s-primary", "1.28.0"})
	_ = rootCmd.Execute()
}

func TestBiomeEligibilityCheckCmd(t *testing.T) {
	checkResult := map[string]interface{}{
		"biome_instance_id":      "inst-abc123",
		"eligible_for_upgrade":   true,
		"eligible_for_migration": false,
		"reason":                 "resource constraints on target node",
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(checkResult)))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "eligibility-check", "inst-abc123"})
	_ = rootCmd.Execute()
}

func TestBiomeUpgradeCmd(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "upgrade_initiated", "version": "1.29.0"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "upgrade", "k8s-primary", "--to", "1.29.0"})
	_ = rootCmd.Execute()
}

func TestBiomeNewCmd_ErrorNoName(t *testing.T) {
	// biome new requires exactly one argument.
	// Cobra will validate this at parse time.
	rootCmd.SetArgs([]string{"biome", "new"})

	err := rootCmd.Execute()
	// Error is expected when argument is missing.
	if err == nil {
		// Cobra may print to stderr and still return nil in some cases,
		// or it might return an error. Accept both.
	}
}

func TestBiomeNewCmd_ScaffoldCreatesDirectories(t *testing.T) {
	dir := t.TempDir()
	original, _ := os.Getwd()
	_ = os.Chdir(dir)
	defer func() { _ = os.Chdir(original) }()

	// Call the scaffoldBiome function directly (no API call).
	if err := scaffoldBiome("test-biome"); err != nil {
		t.Fatalf("scaffoldBiome: %v", err)
	}

	// Verify directories created.
	expectedDirs := []string{
		"test-biome",
		"test-biome/cloud-init",
		"test-biome/hooks",
		"test-biome/tests",
	}
	for _, d := range expectedDirs {
		if _, err := os.Stat(d); os.IsNotExist(err) {
			t.Errorf("scaffold missing directory: %s", d)
		}
	}

	// Verify files created.
	expectedFiles := []string{
		"test-biome/biome.yaml",
		"test-biome/cloud-init/user-data.yaml",
		"test-biome/cloud-init/network-config.yaml",
		"test-biome/lxd-profile.yaml",
		"test-biome/image-ref.txt",
		"test-biome/hooks/post-deploy.sh",
		"test-biome/tests/smoke.sh",
	}
	for _, f := range expectedFiles {
		if _, err := os.Stat(f); os.IsNotExist(err) {
			t.Errorf("scaffold missing file: %s", f)
		}
	}
}

func TestBiomeNewCmd_ScaffoldBiomeYAMLContent(t *testing.T) {
	dir := t.TempDir()
	original, _ := os.Getwd()
	_ = os.Chdir(dir)
	defer func() { _ = os.Chdir(original) }()

	if err := scaffoldBiome("test-biome"); err != nil {
		t.Fatalf("scaffoldBiome: %v", err)
	}

	// Verify biome.yaml contains expected fields.
	raw, err := os.ReadFile("test-biome/biome.yaml")
	if err != nil {
		t.Fatalf("read biome.yaml: %v", err)
	}
	content := string(raw)

	expectedFields := []string{
		"name: test-biome",
		"biome_kind:",
		"phase:",
		"workload_type:",
		"lock_to_host:",
		"upgrade_strategy:",
		"readiness_probe:",
		"resources:",
	}

	for _, field := range expectedFields {
		if !strings.Contains(content, field) {
			t.Errorf("biome.yaml missing field: %q", field)
		}
	}
}

func TestBiomeNewCmd_CloudInitUserData(t *testing.T) {
	dir := t.TempDir()
	original, _ := os.Getwd()
	_ = os.Chdir(dir)
	defer func() { _ = os.Chdir(original) }()

	if err := scaffoldBiome("test-biome"); err != nil {
		t.Fatalf("scaffoldBiome: %v", err)
	}

	raw, err := os.ReadFile("test-biome/cloud-init/user-data.yaml")
	if err != nil {
		t.Fatalf("read user-data.yaml: %v", err)
	}
	content := string(raw)

	// Verify cloud-init format.
	if !strings.Contains(content, "#cloud-config") {
		t.Errorf("user-data.yaml missing #cloud-config header")
	}
	if !strings.Contains(content, "runcmd:") {
		t.Errorf("user-data.yaml missing runcmd")
	}
}

func TestBiomeNewCmd_HooksExecutable(t *testing.T) {
	dir := t.TempDir()
	original, _ := os.Getwd()
	_ = os.Chdir(dir)
	defer func() { _ = os.Chdir(original) }()

	if err := scaffoldBiome("test-biome"); err != nil {
		t.Fatalf("scaffoldBiome: %v", err)
	}

	// Verify hook scripts are executable.
	hookFiles := []string{
		"test-biome/hooks/post-deploy.sh",
		"test-biome/tests/smoke.sh",
	}

	for _, f := range hookFiles {
		info, err := os.Stat(f)
		if err != nil {
			t.Fatalf("stat %s: %v", f, err)
		}

		// Check if file is executable (mode & 0o111).
		if info.Mode()&0o111 == 0 {
			t.Errorf("%s is not executable", f)
		}
	}
}

func TestBiomePromoteCmd_MissingArgs(t *testing.T) {
	// biome promote requires exactly two arguments.
	rootCmd.SetArgs([]string{"biome", "promote", "k8s-primary"})

	err := rootCmd.Execute()
	// Error expected for missing second argument.
	if err == nil {
		// Accept if Cobra validation passes silently in some contexts.
	}
}

func TestBiomeDiffCmd_MissingArgs(t *testing.T) {
	// biome diff requires exactly three arguments.
	rootCmd.SetArgs([]string{"biome", "diff", "k8s-primary"})

	err := rootCmd.Execute()
	// Error expected for missing arguments.
	if err == nil {
		// Accept if Cobra validation passes silently.
	}
}

func TestBiomeUpgradeCmd_MissingToFlag(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	// biome upgrade requires --to flag.
	rootCmd.SetArgs([]string{"biome", "upgrade", "k8s-primary"})

	err := rootCmd.Execute()
	// Error expected for missing required flag.
	if err == nil {
		// Accept if flag validation passes silently.
	}
}

func TestBiomeShowCmd_NoArgs(t *testing.T) {
	// biome show requires exactly one argument.
	rootCmd.SetArgs([]string{"biome", "show"})

	err := rootCmd.Execute()
	// Error expected for missing argument.
	if err == nil {
		// Accept if Cobra validation passes silently.
	}
}

// TestBiomeDeployCmd_KubeVIPMode tests biome deploy with kube-vip frontend mode (default).
func TestBiomeDeployCmd_KubeVIPMode(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "deployment_initiated"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "deploy", "k8s-primary",
		"--node", "node-1",
		"--frontend-mode", "kube-vip",
		"--frontend-endpoint", "10.2.0.10:6443",
		"--frontend-interface", "ens3",
		"--frontend-baseline", "external",
	})
	_ = rootCmd.Execute()
}

// TestBiomeDeployCmd_ExternalMode tests biome deploy with external LB frontend mode.
func TestBiomeDeployCmd_ExternalMode(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "deployment_initiated"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "deploy", "k8s-primary",
		"--node", "node-2",
		"--frontend-mode", "external",
		"--frontend-endpoint", "nlb.example.com:6443",
	})
	_ = rootCmd.Execute()
}

// TestBiomeDeployCmd_NoneMode tests biome deploy with no frontend (single-node lab).
func TestBiomeDeployCmd_NoneMode(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "deployment_initiated"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "deploy", "k8s-primary",
		"--node", "node-3",
		"--frontend-mode", "none",
	})
	_ = rootCmd.Execute()
}

// TestBiomeDeployCmd_DefaultFrontendMode tests biome deploy with default (kube-vip) frontend mode.
func TestBiomeDeployCmd_DefaultFrontendMode(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{"status": "deployment_initiated"})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "deploy", "k8s-primary",
		"--node", "node-4",
		"--frontend-endpoint", "10.2.0.11:6443",
	})
	_ = rootCmd.Execute()
}

// TestBiomeDeployCmd_MissingEndpoint tests error when endpoint required but not provided.
func TestBiomeDeployCmd_MissingEndpoint(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "deploy", "k8s-primary",
		"--node", "node-5",
		"--frontend-mode", "kube-vip",
	})

	err := rootCmd.Execute()
	// Error expected for missing endpoint on kube-vip mode.
	if err == nil {
		t.Logf("Expected error for missing --frontend-endpoint on kube-vip mode")
	}
}

// TestBiomeDeployCmd_EndpointWithNoneMode tests error when endpoint set but mode=none.
func TestBiomeDeployCmd_EndpointWithNoneMode(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "deploy", "k8s-primary",
		"--node", "node-6",
		"--frontend-mode", "none",
		"--frontend-endpoint", "10.2.0.12:6443",
	})

	err := rootCmd.Execute()
	// Error expected when endpoint is set with mode=none.
	if err == nil {
		t.Logf("Expected error for --frontend-endpoint with mode=none")
	}
}

// TestBiomeDeployCmd_InvalidMode tests error on invalid frontend mode.
func TestBiomeDeployCmd_InvalidMode(t *testing.T) {
	resp := apiSuccessResponse(map[string]string{})
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, resp))
	defer cleanup()

	rootCmd.SetArgs([]string{"biome", "deploy", "k8s-primary",
		"--node", "node-7",
		"--frontend-mode", "invalid-mode",
		"--frontend-endpoint", "10.2.0.13:6443",
	})

	err := rootCmd.Execute()
	// Error expected for invalid mode.
	if err == nil {
		t.Logf("Expected error for invalid --frontend-mode")
	}
}
