//go:build noxdp

package cmd

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/penguintechinc/gough/cli/gough/internal/client"
)

// TestClusterUpgrade tests the cluster upgrade command
func TestClusterUpgrade(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "upgrade"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster upgrade: %v", err)
	}
}

// TestClusterEvacuate tests the cluster evacuate command
func TestClusterEvacuate(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "evacuation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "evacuate", "node-123"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster evacuate: %v", err)
	}
}

// TestClusterAdoptK8s tests adopting a k8s cluster
func TestClusterAdoptK8s(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "adopt queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "adopt", "k8s"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster adopt k8s: %v", err)
	}
}

// TestClusterAdoptLXD tests adopting an LXD cluster
func TestClusterAdoptLXD(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "adopt queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "adopt", "lxd"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster adopt lxd: %v", err)
	}
}

// TestClusterAdoptCeph tests adopting a Ceph cluster
func TestClusterAdoptCeph(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "adopt queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "adopt", "ceph"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster adopt ceph: %v", err)
	}
}

// TestClusterAdoptLonghorn tests adopting a Longhorn cluster
func TestClusterAdoptLonghorn(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "adopt queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "adopt", "longhorn"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster adopt longhorn: %v", err)
	}
}

// TestClusterAdoptInvalidKind tests that invalid adopt kind errors without API call
func TestClusterAdoptInvalidKind(t *testing.T) {
	// Set up a server that should NOT be called
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("API should not be called for invalid adopt kind")
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token-abc123xyz")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)

	rootCmd.SetArgs([]string{"cluster", "adopt", "invalid-kind"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err == nil {
		t.Fatalf("cluster adopt invalid: expected error, got nil")
	}
}

// TestClusterRotateJoinerSecrets tests rotating joiner secrets
func TestClusterRotateJoinerSecrets(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "rotation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "rotate-joiner-secrets"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster rotate-joiner-secrets: %v", err)
	}
}

// TestClusterIdentityPlaneStatus tests identity plane status
func TestClusterIdentityPlaneStatus(t *testing.T) {
	status := map[string]interface{}{
		"provider": "builtin",
		"healthy":  true,
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(status)))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "identity-plane", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster identity-plane status: %v", err)
	}
}

// TestClusterIdentityPlaneConfigure tests configuring the identity plane
func TestClusterIdentityPlaneConfigure(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "configuration queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "identity-plane", "configure", "--provider", "builtin"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster identity-plane configure: %v", err)
	}
}

// TestClusterNetworkBaselineStatus tests network baseline status
func TestClusterNetworkBaselineStatus(t *testing.T) {
	status := map[string]interface{}{
		"mgmt":     map[string]string{"cidr": "10.0.0.0/24"},
		"internal": map[string]string{"cidr": "10.1.0.0/24"},
		"external": map[string]string{"cidr": "10.2.0.0/24"},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(status)))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster network-baseline status: %v", err)
	}
}

// TestClusterNetworkBaselineConfigure tests configuring a network baseline
func TestClusterNetworkBaselineConfigure(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "baseline configuration queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "configure", "mgmt", "--opt", "cidr=10.0.0.0/24"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster network-baseline configure: %v", err)
	}
}

// TestClusterNetworkBaselineMigrate tests migrating a network baseline
func TestClusterNetworkBaselineMigrate(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "migration queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "migrate", "mgmt", "--to", "squawk"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster network-baseline migrate: %v", err)
	}
}

// TestClusterTagVocabulary tests getting the tag vocabulary
func TestClusterTagVocabulary(t *testing.T) {
	vocab := map[string]interface{}{
		"env": []string{"prod", "staging", "dev"},
		"tier": []string{"standard", "premium"},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(vocab)))
	defer cleanup()

	rootCmd.SetArgs([]string{"cluster", "tag-vocabulary"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("cluster tag-vocabulary: %v", err)
	}
}

// TestLXDShowTrustPassword tests retrieving the LXD trust password
func TestLXDShowTrustPassword(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"password": "secret-trust-password-123",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"lxd", "show-trust-password", "--reason", "testing"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("lxd show-trust-password: %v", err)
	}
}

// TestLXDRotateTrustPassword tests rotating the LXD trust password
func TestLXDRotateTrustPassword(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "rotation queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"lxd", "rotate-trust-password"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("lxd rotate-trust-password: %v", err)
	}
}

// TestDiskListEmpty tests disk list with no disks
func TestDiskListEmpty(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]client.DiskInfo{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"disk", "list", "node-123"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("disk list empty: %v", err)
	}
}

// TestDiskListWithDisks tests disk list with multiple disks
func TestDiskListWithDisks(t *testing.T) {
	disks := []client.DiskInfo{
		{
			ID:         "disk-1",
			Device:     "/dev/sda",
			SizeGB:     1000,
			Kind:       "ssd",
			DarkDrive:  false,
			SmartState: "healthy",
			Model:      "Samsung SSD 970",
		},
		{
			ID:         "disk-2",
			Device:     "/dev/sdb",
			SizeGB:     2000,
			Kind:       "hdd",
			DarkDrive:  true,
			SmartState: "healthy",
			Model:      "WD Red Pro",
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(disks)))
	defer cleanup()

	rootCmd.SetArgs([]string{"disk", "list", "node-123"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("disk list with disks: %v", err)
	}
}

// TestDiskSmartRecheck tests SMART recheck command
func TestDiskSmartRecheck(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "recheck queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"disk", "smart-recheck", "node-123", "disk-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("disk smart-recheck: %v", err)
	}
}

// TestDiskPlanMissingRequiredFlag tests disk plan without required --plan flag
func TestDiskPlanMissingRequiredFlag(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, client.APIResponse{}))
	defer cleanup()

	rootCmd.SetArgs([]string{"disk", "plan", "node-123"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err == nil {
		t.Fatalf("disk plan: expected error for missing --plan flag, got nil")
	}
}

// TestDeploymentListEmpty tests deployment list with no deployments
func TestDeploymentListEmpty(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]client.Deployment{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"deployment", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("deployment list empty: %v", err)
	}
}

// TestDeploymentListWithDeployments tests deployment list with multiple deployments
func TestDeploymentListWithDeployments(t *testing.T) {
	deployments := []client.Deployment{
		{
			ID:        "dep-1",
			BiomeName: "k8s-primary",
			BiomeID:   1,
			NodeID:    1,
			Status:    "succeeded",
			Phase:     3,
			CreatedAt: "2026-04-30T10:00:00Z",
		},
		{
			ID:        "dep-2",
			BiomeName: "lxd-infra",
			BiomeID:   2,
			NodeID:    2,
			Status:    "in_progress",
			Phase:     2,
			CreatedAt: "2026-04-30T11:00:00Z",
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(deployments)))
	defer cleanup()

	rootCmd.SetArgs([]string{"deployment", "list"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("deployment list with deployments: %v", err)
	}
}

// TestDeploymentListWithFilters tests deployment list with filters
func TestDeploymentListWithFilters(t *testing.T) {
	deployments := []client.Deployment{
		{
			ID:        "dep-1",
			BiomeName: "k8s-primary",
			BiomeID:   1,
			NodeID:    1,
			Status:    "succeeded",
			Phase:     3,
			CreatedAt: "2026-04-30T10:00:00Z",
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(deployments)))
	defer cleanup()

	rootCmd.SetArgs([]string{"deployment", "list", "--status", "succeeded", "--biome-id", "k8s-primary"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("deployment list with filters: %v", err)
	}
}

// TestDeploymentLogsEmpty tests deployment logs with no logs
func TestDeploymentLogsEmpty(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse([]client.DeploymentLog{})))
	defer cleanup()

	rootCmd.SetArgs([]string{"deployment", "logs", "dep-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("deployment logs empty: %v", err)
	}
}

// TestDeploymentLogsWithEntries tests deployment logs with log entries
func TestDeploymentLogsWithEntries(t *testing.T) {
	logs := []client.DeploymentLog{
		{
			ID:        "log-1",
			Message:   "Starting deployment",
			Level:     "info",
			CreatedAt: "2026-04-30T10:00:00Z",
		},
		{
			ID:        "log-2",
			Message:   "Pulling biome image",
			Level:     "info",
			CreatedAt: "2026-04-30T10:01:00Z",
		},
		{
			ID:        "log-3",
			Message:   "Deployment succeeded",
			Level:     "info",
			CreatedAt: "2026-04-30T10:02:00Z",
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(logs)))
	defer cleanup()

	rootCmd.SetArgs([]string{"deployment", "logs", "dep-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("deployment logs with entries: %v", err)
	}
}

// TestDeploymentLogsWithTail tests deployment logs with --tail flag
func TestDeploymentLogsWithTail(t *testing.T) {
	logs := []client.DeploymentLog{
		{
			ID:        "log-1",
			Message:   "Final log entry",
			Level:     "info",
			CreatedAt: "2026-04-30T10:05:00Z",
		},
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(logs)))
	defer cleanup()

	rootCmd.SetArgs([]string{"deployment", "logs", "dep-1", "--tail", "10"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("deployment logs with --tail: %v", err)
	}
}

// TestDeploymentCancel tests cancelling a deployment
func TestDeploymentCancel(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(map[string]string{
		"status": "cancelled",
	})))
	defer cleanup()

	rootCmd.SetArgs([]string{"deployment", "cancel", "dep-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("deployment cancel: %v", err)
	}
}

// TestAuditVerifyWithDates tests audit verify with date range
func TestAuditVerifyWithDates(t *testing.T) {
	result := map[string]interface{}{
		"valid":        true,
		"chain_intact": true,
	}
	_, cleanup := setupTestEnv(t, jsonResponse(t, 200, apiSuccessResponse(result)))
	defer cleanup()

	rootCmd.SetArgs([]string{"audit", "verify", "--since", "2026-01-01", "--to", "2026-04-30"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("audit verify: %v", err)
	}
}

// TestAuditVerifyDeferred tests audit verify with deferred response
func TestAuditVerifyDeferred(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "verification in progress",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"audit", "verify", "--since", "2026-01-01"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("audit verify deferred: %v", err)
	}
}

// TestAuditExportWithSince tests audit export with since flag
func TestAuditExportWithSince(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "export queued",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"audit", "export", "--since", "2026-01-01", "--output", "/tmp/audit.jsonl"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("audit export: %v", err)
	}
}

// TestAuditExportSuccess tests audit export with all required flags
func TestAuditExportSuccess(t *testing.T) {
	_, cleanup := setupTestEnv(t, jsonResponse(t, 202, client.APIResponse{
		Status: "deferred",
		Note:   "export queued for processing",
	}))
	defer cleanup()

	rootCmd.SetArgs([]string{"audit", "export", "--since", "2026-01-01", "--output", "/tmp/audit-full.jsonl"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	err := rootCmd.Execute()
	if err != nil {
		t.Fatalf("audit export success: %v", err)
	}
}
