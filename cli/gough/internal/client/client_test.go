//go:build noxdp

package client

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

// newTestServer creates an httptest server that returns the given status code
// and JSON body for all requests.
func newTestServer(t *testing.T, status int, body interface{}) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		if body != nil {
			_ = json.NewEncoder(w).Encode(body)
		}
	}))
}

// newTestClient creates a Client pointed at the given test server's URL.
func newTestClient(server *httptest.Server) *Client {
	// Strip the trailing /api/v1 that New() would add; test server expects
	// requests at the test server's base URL.
	c := New(server.URL, "test-token-abc123xyz")
	// Override base URL to strip the appended /api/v1.
	c.baseURL = server.URL + "/api/v1"
	return c
}

func TestListNodes_Success(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]Node{{ID: "n1", Hostname: "node01", State: "ready"}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	nodes, err := c.ListNodes(context.Background(), NodeListParams{})
	if err != nil {
		t.Fatalf("ListNodes returned error: %v", err)
	}
	if len(nodes) != 1 {
		t.Fatalf("expected 1 node, got %d", len(nodes))
	}
	if nodes[0].ID != "n1" {
		t.Errorf("node ID = %q; want n1", nodes[0].ID)
	}
}

func TestListNodes_RateLimit(t *testing.T) {
	srv := newTestServer(t, 429, map[string]string{"error": "rate limited"})
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error for 429, got nil")
	}
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.ExitCode() != 5 {
		t.Errorf("exit code = %d; want 5", apiErr.ExitCode())
	}
}

func TestListNodes_Unauthorized(t *testing.T) {
	srv := newTestServer(t, 401, map[string]string{"error": "unauthorized"})
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error for 401, got nil")
	}
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.ExitCode() != 3 {
		t.Errorf("exit code = %d; want 3", apiErr.ExitCode())
	}
}

func TestListNodes_TenantMismatch(t *testing.T) {
	srv := newTestServer(t, 403, map[string]string{"error": "tenant mismatch"})
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error for 403 tenant, got nil")
	}
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.ExitCode() != 4 {
		t.Errorf("exit code = %d; want 4", apiErr.ExitCode())
	}
}

func TestGetClusterStatus_Success(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data: mustMarshal(ClusterStatus{
			ClusterID:  "cluster-1",
			State:      "healthy",
			NodeCount:  5,
			ReadyNodes: 5,
		}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	status, err := c.GetClusterStatus(context.Background())
	if err != nil {
		t.Fatalf("GetClusterStatus error: %v", err)
	}
	if status.ClusterID != "cluster-1" {
		t.Errorf("cluster_id = %q; want cluster-1", status.ClusterID)
	}
}

func TestDeferred_202(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "operation queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	resp, err := c.EvacuateNode(context.Background(), "n1")
	if err != nil {
		t.Fatalf("EvacuateNode error: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("expected deferred response")
	}
	if resp.Note != "operation queued" {
		t.Errorf("note = %q; want 'operation queued'", resp.Note)
	}
}

func TestDecodeData(t *testing.T) {
	resp := &APIResponse{
		Data: mustMarshal(map[string]string{"foo": "bar"}),
	}
	var out map[string]string
	if err := DecodeData(resp, &out); err != nil {
		t.Fatalf("DecodeData error: %v", err)
	}
	if out["foo"] != "bar" {
		t.Errorf("foo = %q; want bar", out["foo"])
	}
}

func TestDecodeData_Nil(t *testing.T) {
	resp := &APIResponse{}
	var out map[string]string
	if err := DecodeData(resp, &out); err == nil {
		t.Error("expected error for nil data, got nil")
	}
}

func TestAPIError_Error(t *testing.T) {
	err := &APIError{Code: 6, Message: "validation failed"}
	if err.Error() != "validation failed" {
		t.Errorf("Error() = %q", err.Error())
	}
	if err.ExitCode() != 6 {
		t.Errorf("ExitCode() = %d; want 6", err.ExitCode())
	}
}

func TestListBiomes_FilterParams(t *testing.T) {
	var gotURL string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotURL = r.URL.String()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal([]Biome{}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	_, _ = c.ListBiomes(context.Background(), ListBiomesParams{Kind: "infrastructure"})
	if gotURL == "" || !containsParam(gotURL, "biome_kind=infrastructure") {
		t.Errorf("expected biome_kind=infrastructure in URL, got %q", gotURL)
	}
}

func TestGetCapacityForecast(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data: mustMarshal(CapacityForecast{
			HorizonDays: 7,
			Nodes: []NodeForecast{
				{NodeID: "n1", BreachRisk: "low"},
			},
		}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	fc, err := c.GetCapacityForecast(context.Background(), 7)
	if err != nil {
		t.Fatalf("GetCapacityForecast error: %v", err)
	}
	if len(fc.Nodes) != 1 {
		t.Errorf("expected 1 node forecast, got %d", len(fc.Nodes))
	}
}

func TestGetMigrationPolicy(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal(MigrationPolicy{MinHealthyNodes: 3, MaxConcurrentMigrations: 1}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	p, err := c.GetMigrationPolicy(context.Background())
	if err != nil {
		t.Fatalf("GetMigrationPolicy error: %v", err)
	}
	if p.MinHealthyNodes != 3 {
		t.Errorf("MinHealthyNodes = %d; want 3", p.MinHealthyNodes)
	}
}

func TestIsDeferred(t *testing.T) {
	cases := []struct {
		resp *APIResponse
		want bool
	}{
		{&APIResponse{Status: "deferred"}, true},
		{&APIResponse{Note: "queued"}, true},
		{&APIResponse{Status: "success"}, false},
		{&APIResponse{}, false},
	}
	for _, tc := range cases {
		got := IsDeferred(tc.resp)
		if got != tc.want {
			t.Errorf("IsDeferred(%+v) = %v; want %v", tc.resp, got, tc.want)
		}
	}
}

// helpers

func mustMarshal(v interface{}) json.RawMessage {
	b, _ := json.Marshal(v)
	return b
}

func containsParam(url, param string) bool {
	return len(url) > 0 && (len(param) == 0 || containsStr(url, param))
}

func containsStr(s, sub string) bool {
	return len(s) >= len(sub) && (s == sub || findStr(s, sub))
}

func findStr(s, sub string) bool {
	for i := 0; i <= len(s)-len(sub); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

// Additional tests for uncovered methods

func TestSetTenantID(t *testing.T) {
	c := New("http://localhost:8080", "test-token")
	c.SetTenantID("tenant-123")
	if c.tenantID != "tenant-123" {
		t.Errorf("SetTenantID failed: got %q, want %q", c.tenantID, "tenant-123")
	}
}

func TestPatch(t *testing.T) {
	called := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPatch {
			t.Errorf("expected PATCH, got %s", r.Method)
		}
		called = true
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"status": "updated"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.SetMigrationPolicy(ctx, "max_concurrent_migrations", "2")
	if err != nil {
		t.Fatalf("SetMigrationPolicy: %v", err)
	}
	if !called {
		t.Error("PATCH endpoint was not called")
	}
}

func TestDel(t *testing.T) {
	called := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Errorf("expected DELETE, got %s", r.Method)
		}
		called = true
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"status": "removed"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.RemoveNodeTag(ctx, "node-1", "tagkey", "tagval")
	if err != nil {
		t.Fatalf("RemoveNodeTag: %v", err)
	}
	if !called {
		t.Error("DELETE endpoint was not called")
	}
}

func TestListDisks(t *testing.T) {
	disks := []DiskInfo{
		{
			ID:         "disk-1",
			NodeID:     "node-1",
			Device:     "/dev/sda",
			SizeGB:     1000,
			Kind:       "ssd",
			SmartState: "ok",
		},
	}
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(disks),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	result, err := c.ListDisks(ctx, "node-1")
	if err != nil {
		t.Fatalf("ListDisks: %v", err)
	}
	if len(result) != 1 {
		t.Errorf("expected 1 disk, got %d", len(result))
	}
	if result[0].ID != "disk-1" {
		t.Errorf("disk ID: got %q, want %q", result[0].ID, "disk-1")
	}
}

func TestPlanDisks(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"status": "planned"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	plan := map[string]interface{}{"disks": []string{"disk-1"}}
	_, err := c.PlanDisks(ctx, "node-1", plan)
	if err != nil {
		t.Fatalf("PlanDisks: %v", err)
	}
}

func TestSmartRecheck(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]string{"status": "rechecked"}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.SmartRecheck(ctx, "node-1", "disk-1")
	if err != nil {
		t.Fatalf("SmartRecheck: %v", err)
	}
}

func TestListDeployments(t *testing.T) {
	deployments := []Deployment{
		{
			ID:        "deploy-1",
			BiomeID:   1,
			NodeID:    2,
			Status:    "succeeded",
			CreatedAt: "2026-01-01T00:00:00Z",
		},
	}
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(deployments),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	result, err := c.ListDeployments(ctx, DeploymentListParams{})
	if err != nil {
		t.Fatalf("ListDeployments: %v", err)
	}
	if len(result) != 1 {
		t.Errorf("expected 1 deployment, got %d", len(result))
	}
}

func TestGetDeploymentLogs(t *testing.T) {
	logs := []DeploymentLog{
		{
			ID:           "log-1",
			DeploymentID: "deploy-1",
			Message:      "deployment started",
			Level:        "info",
			CreatedAt:    "2026-01-01T00:00:00Z",
		},
	}
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(logs),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	result, err := c.GetDeploymentLogs(ctx, "deploy-1", 10)
	if err != nil {
		t.Fatalf("GetDeploymentLogs: %v", err)
	}
	if len(result) != 1 {
		t.Errorf("expected 1 log entry, got %d", len(result))
	}
}

func TestCancelDeployment(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"status": "cancelled"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.CancelDeployment(ctx, "deploy-1")
	if err != nil {
		t.Fatalf("CancelDeployment: %v", err)
	}
}

func TestListStorageQuotas(t *testing.T) {
	quotas := []StorageQuota{
		{
			ID:           "quota-1",
			TenantID:     "tenant-123",
			ResourceType: "block_storage",
			LimitValue:   100.0,
			UsedValue:    50.0,
			Unit:         "GiB",
			UpdatedAt:    "2026-01-01T00:00:00Z",
		},
	}
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(quotas),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	result, err := c.ListStorageQuotas(ctx, "")
	if err != nil {
		t.Fatalf("ListStorageQuotas: %v", err)
	}
	if len(result) != 1 {
		t.Errorf("expected 1 quota, got %d", len(result))
	}
}

func TestRequestStorageQuota(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"request_id": "req-123"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.RequestStorageQuota(ctx, StorageQuotaRequestParams{
		TenantID:       "tenant-123",
		ResourceType:   "block_storage",
		RequestedValue: 100.0,
		Unit:           "GiB",
	})
	if err != nil {
		t.Fatalf("RequestStorageQuota: %v", err)
	}
}

func TestConfigureIntegration(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"status": "configured"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	cfg := map[string]string{"endpoint": "https://example.com"}
	_, err := c.ConfigureIntegration(ctx, "squawk", cfg)
	if err != nil {
		t.Fatalf("ConfigureIntegration: %v", err)
	}
}

func TestRotateIntegrationCredentials(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"status": "rotated"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.RotateIntegrationCredentials(ctx, "squawk")
	if err != nil {
		t.Fatalf("RotateIntegrationCredentials: %v", err)
	}
}

func TestValidateIntegrationScope(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"valid": true, "scopes": []string{"read", "write"}}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.ValidateIntegrationScope(ctx, "squawk")
	if err != nil {
		t.Fatalf("ValidateIntegrationScope: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("response status: got %q, want %q", resp.Status, "success")
	}
}

func TestTestWebhook(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"status": "tested"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.TestWebhook(ctx, "webhook-1")
	if err != nil {
		t.Fatalf("TestWebhook: %v", err)
	}
}

func TestStartNodeSim(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"status": "started"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.StartNodeSim(ctx, NodeSimParams{
		Arch:     "amd64",
		Firmware: "uefi",
		IPv6:     false,
	})
	if err != nil {
		t.Fatalf("StartNodeSim: %v", err)
	}
}

func TestDevSeed(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"status": "seeding"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.DevSeed(ctx, "k8s-primary,nest-agent")
	if err != nil {
		t.Fatalf("DevSeed: %v", err)
	}
}

func TestDevReset(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(APIResponse{
			Status: "success",
			Data:   mustMarshal(map[string]string{"status": "reset"}),
		})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.DevReset(ctx)
	if err != nil {
		t.Fatalf("DevReset: %v", err)
	}
}

func TestInit(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "bootstrap initiated",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.Init(ctx, InitParams{
		HA:                false,
		DHCPAuthoritative: false,
	})
	if err != nil {
		t.Fatalf("Init: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("Init should return deferred response")
	}
}

func TestRunDoctorCheck(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]string{"status": "healthy"}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	_, err := c.RunDoctorCheck(ctx, "network", map[string]string{})
	if err != nil {
		t.Fatalf("RunDoctorCheck: %v", err)
	}
}

func TestSyncExport(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "export queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.SyncExport(ctx, "/tmp/bundle")
	if err != nil {
		t.Fatalf("SyncExport: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("SyncExport should return deferred response")
	}
}

func TestSyncImport(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "import queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.SyncImport(ctx, "/tmp/bundle")
	if err != nil {
		t.Fatalf("SyncImport: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("SyncImport should return deferred response")
	}
}

func TestListIntegrations(t *testing.T) {
	integrations := []Integration{
		{
			Name:     "squawk",
			Status:   "healthy",
			Version:  "2.0.0",
			Endpoint: "https://squawk.example.com",
			LastPing: "2026-01-01T00:00:00Z",
			ScopeOK:  true,
		},
	}
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(integrations),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	result, err := c.ListIntegrations(ctx)
	if err != nil {
		t.Fatalf("ListIntegrations: %v", err)
	}
	if len(result) != 1 {
		t.Errorf("expected 1 integration, got %d", len(result))
	}
	if result[0].Name != "squawk" {
		t.Errorf("integration name: got %q, want %q", result[0].Name, "squawk")
	}
}

// Tests for biome operations
func TestValidateBiome(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"valid": true}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.ValidateBiome(ctx, false)
	if err != nil {
		t.Fatalf("ValidateBiome: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}
}

func TestPublishBiome(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "publish queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.PublishBiome(ctx)
	if err != nil {
		t.Fatalf("PublishBiome: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("PublishBiome should return deferred response")
	}
}

func TestPromoteBiome(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "promotion queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.PromoteBiome(ctx, "k8s-primary", "1.2.0")
	if err != nil {
		t.Fatalf("PromoteBiome: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("PromoteBiome should return deferred response")
	}
}

func TestRollbackBiome(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "rollback queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.RollbackBiome(ctx, "k8s-primary")
	if err != nil {
		t.Fatalf("RollbackBiome: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("RollbackBiome should return deferred response")
	}
}

func TestDiffBiomes(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"diff": "content"}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.DiffBiomes(ctx, "k8s-primary", "1.0.0", "1.1.0")
	if err != nil {
		t.Fatalf("DiffBiomes: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}
}

func TestReSignBiome(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "re-sign queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.ReSignBiome(ctx, "k8s-primary", "1.2.0")
	if err != nil {
		t.Fatalf("ReSignBiome: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("ReSignBiome should return deferred response")
	}
}

func TestUpgradeBiome(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "upgrade queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.UpgradeBiome(ctx, "k8s-primary", "1.3.0")
	if err != nil {
		t.Fatalf("UpgradeBiome: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("UpgradeBiome should return deferred response")
	}
}

func TestBiomeEligibilityCheck(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"eligible": true}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.BiomeEligibilityCheck(ctx, "biome-123")
	if err != nil {
		t.Fatalf("BiomeEligibilityCheck: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}
}

// Tests for cluster operations
func TestUpgradeCluster(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "cluster upgrade queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.UpgradeCluster(ctx)
	if err != nil {
		t.Fatalf("UpgradeCluster: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("UpgradeCluster should return deferred response")
	}
}

func TestEvacuateCluster(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "evacuation queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.EvacuateCluster(ctx, "node-1")
	if err != nil {
		t.Fatalf("EvacuateCluster: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("EvacuateCluster should return deferred response")
	}
}

func TestAdoptCluster(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "adoption queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	opts := map[string]string{"url": "k8s://api.example.com"}
	resp, err := c.AdoptCluster(ctx, "k8s", opts)
	if err != nil {
		t.Fatalf("AdoptCluster: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("AdoptCluster should return deferred response")
	}
}

func TestRotateJoinerSecrets(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "rotation queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.RotateJoinerSecrets(ctx, "k8s-primary")
	if err != nil {
		t.Fatalf("RotateJoinerSecrets: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("RotateJoinerSecrets should return deferred response")
	}
}

func TestLXDShowTrustPassword(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"password": "secret"}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.LXDShowTrustPassword(ctx, "manual rotation")
	if err != nil {
		t.Fatalf("LXDShowTrustPassword: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}
}

func TestLXDRotateTrustPassword(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "rotation queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.LXDRotateTrustPassword(ctx)
	if err != nil {
		t.Fatalf("LXDRotateTrustPassword: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("LXDRotateTrustPassword should return deferred response")
	}
}

// Tests for identity plane
func TestIdentityPlaneStatus(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"provider": "spire", "healthy": true}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.IdentityPlaneStatus(ctx)
	if err != nil {
		t.Fatalf("IdentityPlaneStatus: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}
}

func TestIdentityPlaneConfigure(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "configuration queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.IdentityPlaneConfigure(ctx, "skauswatch")
	if err != nil {
		t.Fatalf("IdentityPlaneConfigure: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("IdentityPlaneConfigure should return deferred response")
	}
}

// Tests for network baseline
func TestNetworkBaselineStatus(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"baseline": "three-tier", "healthy": true}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.NetworkBaselineStatus(ctx)
	if err != nil {
		t.Fatalf("NetworkBaselineStatus: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}
}

func TestNetworkBaselineConfigure(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "configuration queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	opts := map[string]string{"provider": "squawk"}
	resp, err := c.NetworkBaselineConfigure(ctx, "three-tier", opts)
	if err != nil {
		t.Fatalf("NetworkBaselineConfigure: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("NetworkBaselineConfigure should return deferred response")
	}
}

func TestNetworkBaselineMigrate(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "migration queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.NetworkBaselineMigrate(ctx, "three-tier", "squawk")
	if err != nil {
		t.Fatalf("NetworkBaselineMigrate: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("NetworkBaselineMigrate should return deferred response")
	}
}

func TestGetTagVocabulary(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"tags": []string{"tier", "env"}}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.GetTagVocabulary(ctx)
	if err != nil {
		t.Fatalf("GetTagVocabulary: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}
}

// Tests for capacity & migration
func TestGetCapacityRisks(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"risks": []string{"disk_full"}}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.GetCapacityRisks(ctx)
	if err != nil {
		t.Fatalf("GetCapacityRisks: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}
}

func TestTriggerMigration(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "migration queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.TriggerMigration(ctx, "biome-123", "node-2", false, "load balancing")
	if err != nil {
		t.Fatalf("TriggerMigration: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("TriggerMigration should return deferred response")
	}
}

// Tests for DR operations
func TestRunDRDrill(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "drill queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.RunDRDrill(ctx, "site-2")
	if err != nil {
		t.Fatalf("RunDRDrill: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("RunDRDrill should return deferred response")
	}
}

func TestPromoteDRSite(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "promotion queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.PromoteDRSite(ctx, "site-2", "primary failed", true)
	if err != nil {
		t.Fatalf("PromoteDRSite: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("PromoteDRSite should return deferred response")
	}
}

func TestFailbackDRSite(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "failback queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.FailbackDRSite(ctx, "site-2")
	if err != nil {
		t.Fatalf("FailbackDRSite: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("FailbackDRSite should return deferred response")
	}
}

func TestRestoreFromBackup(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "restore queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.RestoreFromBackup(ctx, "s3://bucket/backup.tar.gz", "cluster-2")
	if err != nil {
		t.Fatalf("RestoreFromBackup: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("RestoreFromBackup should return deferred response")
	}
}

// Tests for audit operations
func TestListAuditEvents(t *testing.T) {
	events := []AuditEvent{
		{
			ID:        "event-1",
			Timestamp: "2026-01-01T00:00:00Z",
			Actor:     "user-123",
			Action:    "CreateBiome",
			Resource:  "biome:k8s-primary",
			TenantID:  "tenant-123",
		},
	}
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(events),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	result, err := c.ListAuditEvents(ctx, "2026-01-01T00:00:00Z", "")
	if err != nil {
		t.Fatalf("ListAuditEvents: %v", err)
	}
	if len(result) != 1 {
		t.Errorf("expected 1 event, got %d", len(result))
	}
	if result[0].ID != "event-1" {
		t.Errorf("event ID: got %q, want %q", result[0].ID, "event-1")
	}
}

func TestVerifyAuditChain(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"valid": true}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.VerifyAuditChain(ctx, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
	if err != nil {
		t.Fatalf("VerifyAuditChain: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}
}

func TestExportAuditLog(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "export queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.ExportAuditLog(ctx, "2026-01-01T00:00:00Z", "/tmp/audit.jsonl")
	if err != nil {
		t.Fatalf("ExportAuditLog: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("ExportAuditLog should return deferred response")
	}
}

// Tests for vault operations
func TestVaultUnseal(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"unsealed": false, "progress": 1, "required": 3}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.VaultUnseal(ctx, "fake-shamir-share")
	if err != nil {
		t.Fatalf("VaultUnseal: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}
}

// Tests for primary operations
func TestReplacePrimary(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "replacement queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.ReplacePrimary(ctx, "node-2")
	if err != nil {
		t.Fatalf("ReplacePrimary: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("ReplacePrimary should return deferred response")
	}
}

func TestForceRecoverPrimary(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "recovery queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.ForceRecoverPrimary(ctx, "node-1", "primary crashed")
	if err != nil {
		t.Fatalf("ForceRecoverPrimary: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("ForceRecoverPrimary should return deferred response")
	}
}

func TestSwitchPrimaryFrontend(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "switch queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.SwitchPrimaryFrontend(ctx, "anycast")
	if err != nil {
		t.Fatalf("SwitchPrimaryFrontend: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("SwitchPrimaryFrontend should return deferred response")
	}
}

func TestRotatePrimaryCA(t *testing.T) {
	srv := newTestServer(t, 202, APIResponse{
		Status: "deferred",
		Note:   "rotation queued",
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	resp, err := c.RotatePrimaryCA(ctx)
	if err != nil {
		t.Fatalf("RotatePrimaryCA: %v", err)
	}
	if !IsDeferred(resp) {
		t.Errorf("RotatePrimaryCA should return deferred response")
	}
}

func TestPut_Method(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut {
			t.Errorf("expected PUT, got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(APIResponse{Status: "ok"})
	}))
	defer srv.Close()
	c := &Client{baseURL: srv.URL, token: "t", httpClient: &http.Client{}}
	resp, err := c.put(context.Background(), "/test", map[string]string{"k": "v"})
	if err != nil {
		t.Fatalf("put() error: %v", err)
	}
	if resp.Status != "ok" {
		t.Errorf("Status = %q; want ok", resp.Status)
	}
}

func TestParseErrorBody_Fields(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{`{"error":"err1"}`, "err1"},
		{`{"message":"msg1"}`, "msg1"},
		{`{"detail":"det1"}`, "det1"},
		{`plain text`, "plain text"},
	}
	for _, tt := range tests {
		got := parseErrorBody([]byte(tt.input))
		if got != tt.want {
			t.Errorf("parseErrorBody(%q) = %q; want %q", tt.input, got, tt.want)
		}
	}
}

func TestShowNode(t *testing.T) {
	node := Node{ID: "node-1", Hostname: "server-1", State: "ready"}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(APIResponse{Status: "ok", Data: mustMarshal(node)})
	}))
	defer srv.Close()
	c := &Client{baseURL: srv.URL, token: "t", httpClient: &http.Client{}}
	got, err := c.ShowNode(context.Background(), "node-1")
	if err != nil {
		t.Fatalf("ShowNode() error: %v", err)
	}
	if got.Hostname != "server-1" {
		t.Errorf("Hostname = %q; want server-1", got.Hostname)
	}
}

func TestDeployNode(t *testing.T) {
	srv := newTestServer(t, http.StatusOK, APIResponse{Status: "ok"})
	c := newTestClient(srv)
	defer srv.Close()
	resp, err := c.DeployNode(context.Background(), "node-1", map[string]string{"plan": "default"})
	if err != nil {
		t.Fatalf("DeployNode() error: %v", err)
	}
	_ = resp
}

func TestRejectNode(t *testing.T) {
	srv := newTestServer(t, http.StatusOK, APIResponse{Status: "ok"})
	c := newTestClient(srv)
	defer srv.Close()
	resp, err := c.RejectNode(context.Background(), "node-1", "hardware fault")
	if err != nil {
		t.Fatalf("RejectNode() error: %v", err)
	}
	_ = resp
}

func TestDecommissionNode(t *testing.T) {
	srv := newTestServer(t, http.StatusOK, APIResponse{Status: "ok"})
	c := newTestClient(srv)
	defer srv.Close()
	resp, err := c.DecommissionNode(context.Background(), "node-1", "end of life")
	if err != nil {
		t.Fatalf("DecommissionNode() error: %v", err)
	}
	_ = resp
}

func TestRekeyNode(t *testing.T) {
	srv := newTestServer(t, http.StatusOK, APIResponse{Status: "ok"})
	c := newTestClient(srv)
	defer srv.Close()
	resp, err := c.RekeyNode(context.Background(), "node-1", "routine rotation")
	if err != nil {
		t.Fatalf("RekeyNode() error: %v", err)
	}
	_ = resp
}

func TestAddNodeTag(t *testing.T) {
	srv := newTestServer(t, http.StatusOK, APIResponse{Status: "ok"})
	c := newTestClient(srv)
	defer srv.Close()
	resp, err := c.AddNodeTag(context.Background(), "node-1", "env", "prod")
	if err != nil {
		t.Fatalf("AddNodeTag() error: %v", err)
	}
	_ = resp
}

func TestGetNodeTags(t *testing.T) {
	tags := map[string]string{"env": "prod", "tier": "compute"}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(APIResponse{Status: "ok", Data: mustMarshal(tags)})
	}))
	defer srv.Close()
	c := &Client{baseURL: srv.URL, token: "t", httpClient: &http.Client{}}
	got, err := c.GetNodeTags(context.Background(), "node-1")
	if err != nil {
		t.Fatalf("GetNodeTags() error: %v", err)
	}
	if got["env"] != "prod" {
		t.Errorf("env tag = %q; want prod", got["env"])
	}
}

func TestShowBiome(t *testing.T) {
	biome := Biome{Name: "my-app", Version: "1.2.0"}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(APIResponse{Status: "ok", Data: mustMarshal(biome)})
	}))
	defer srv.Close()
	c := &Client{baseURL: srv.URL, token: "t", httpClient: &http.Client{}}
	got, err := c.ShowBiome(context.Background(), "my-app", "1.2.0")
	if err != nil {
		t.Fatalf("ShowBiome() error: %v", err)
	}
	if got.Name != "my-app" {
		t.Errorf("Name = %q; want my-app", got.Name)
	}
}

func TestHTTPStatus422And503(t *testing.T) {
	for _, tc := range []struct{ code int; wantMsg string }{
		{http.StatusUnprocessableEntity, ""},
		{http.StatusServiceUnavailable, "cluster unhealthy"},
	} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(tc.code)
			_, _ = w.Write([]byte(`{"message":"test error"}`))
		}))
		c := &Client{baseURL: srv.URL, token: "t", httpClient: &http.Client{}}
		_, err := c.ListNodes(context.Background(), NodeListParams{})
		if err == nil {
			t.Errorf("code %d: expected error, got nil", tc.code)
		}
		srv.Close()
	}
}

// TestListNodes_WithStateFilter tests ListNodes with state filter.
func TestListNodes_WithStateFilter(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]Node{{ID: "n1", Hostname: "node01", State: "ready"}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	nodes, err := c.ListNodes(context.Background(), NodeListParams{State: "ready"})
	if err != nil {
		t.Fatalf("ListNodes with state filter: %v", err)
	}
	if len(nodes) != 1 {
		t.Fatalf("expected 1 node, got %d", len(nodes))
	}
}

// TestListNodes_WithTagFilter tests ListNodes with tag filter.
func TestListNodes_WithTagFilter(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]Node{{ID: "n1", Hostname: "node01", State: "ready"}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	nodes, err := c.ListNodes(context.Background(), NodeListParams{Tag: "prod"})
	if err != nil {
		t.Fatalf("ListNodes with tag filter: %v", err)
	}
	if len(nodes) != 1 {
		t.Fatalf("expected 1 node, got %d", len(nodes))
	}
}

// TestListNodes_WithPagination tests ListNodes with pagination params.
func TestListNodes_WithPagination(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]Node{{ID: "n1", Hostname: "node01", State: "ready"}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	nodes, err := c.ListNodes(context.Background(), NodeListParams{Page: 1, Size: 10})
	if err != nil {
		t.Fatalf("ListNodes with pagination: %v", err)
	}
	if len(nodes) != 1 {
		t.Fatalf("expected 1 node, got %d", len(nodes))
	}
}

// TestListNodes_Empty tests ListNodes with empty result.
func TestListNodes_Empty(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]Node{}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	nodes, err := c.ListNodes(context.Background(), NodeListParams{})
	if err != nil {
		t.Fatalf("ListNodes empty: %v", err)
	}
	if len(nodes) != 0 {
		t.Fatalf("expected 0 nodes, got %d", len(nodes))
	}
}

// TestListDeployments_WithFilters tests ListDeployments with various filters.
func TestListDeployments_WithFilters(t *testing.T) {
	deployments := []Deployment{
		{
			ID:        "deploy-1",
			BiomeID:   1,
			NodeID:    2,
			Status:    "succeeded",
			CreatedAt: "2026-01-01T00:00:00Z",
		},
	}
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(deployments),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	result, err := c.ListDeployments(ctx, DeploymentListParams{Status: "succeeded"})
	if err != nil {
		t.Fatalf("ListDeployments with status filter: %v", err)
	}
	if len(result) != 1 {
		t.Errorf("expected 1 deployment, got %d", len(result))
	}
}

// TestListDeployments_WithBiomeIDFilter tests ListDeployments with biome ID filter.
func TestListDeployments_WithBiomeIDFilter(t *testing.T) {
	deployments := []Deployment{
		{
			ID:        "deploy-1",
			BiomeID:   1,
			NodeID:    2,
			Status:    "succeeded",
			CreatedAt: "2026-01-01T00:00:00Z",
		},
	}
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(deployments),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	result, err := c.ListDeployments(ctx, DeploymentListParams{BiomeID: "biome-1"})
	if err != nil {
		t.Fatalf("ListDeployments with biome ID filter: %v", err)
	}
	if len(result) != 1 {
		t.Errorf("expected 1 deployment, got %d", len(result))
	}
}

// TestListDeployments_WithNodeIDFilter tests ListDeployments with node ID filter.
func TestListDeployments_WithNodeIDFilter(t *testing.T) {
	deployments := []Deployment{
		{
			ID:        "deploy-1",
			BiomeID:   1,
			NodeID:    2,
			Status:    "succeeded",
			CreatedAt: "2026-01-01T00:00:00Z",
		},
	}
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal(deployments),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	result, err := c.ListDeployments(ctx, DeploymentListParams{NodeID: "node-1"})
	if err != nil {
		t.Fatalf("ListDeployments with node ID filter: %v", err)
	}
	if len(result) != 1 {
		t.Errorf("expected 1 deployment, got %d", len(result))
	}
}

// TestListDeployments_Empty tests ListDeployments with empty result.
func TestListDeployments_Empty(t *testing.T) {
	srv := newTestServer(t, 200, APIResponse{
		Status: "success",
		Data:   mustMarshal([]Deployment{}),
	})
	defer srv.Close()

	c := newTestClient(srv)
	ctx := context.Background()

	result, err := c.ListDeployments(ctx, DeploymentListParams{})
	if err != nil {
		t.Fatalf("ListDeployments empty: %v", err)
	}
	if len(result) != 0 {
		t.Errorf("expected 0 deployments, got %d", len(result))
	}
}

// TestListBiomes_WithWorkloadFilter tests ListBiomes with workload filter.
func TestListBiomes_WithWorkloadFilter(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]Biome{{Name: "b1", Version: "v1.0", Kind: "lxc"}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	biomes, err := c.ListBiomes(context.Background(), ListBiomesParams{Workload: "lxc"})
	if err != nil {
		t.Fatalf("ListBiomes with workload filter: %v", err)
	}
	if len(biomes) != 1 {
		t.Fatalf("expected 1 biome, got %d", len(biomes))
	}
}

// TestListStorageQuotas_Success tests ListStorageQuotas with success response.
func TestListStorageQuotas_Success(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]StorageQuota{{ID: "sq-1", LimitValue: 100.0}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	quotas, err := c.ListStorageQuotas(context.Background(), "")
	if err != nil {
		t.Fatalf("ListStorageQuotas: %v", err)
	}
	if len(quotas) != 1 {
		t.Fatalf("expected 1 quota, got %d", len(quotas))
	}
}

// TestListStorageQuotas_WithTenant tests ListStorageQuotas with tenant filter.
func TestListStorageQuotas_WithTenant(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]StorageQuota{{ID: "sq-1", LimitValue: 100.0}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	quotas, err := c.ListStorageQuotas(context.Background(), "tenant-1")
	if err != nil {
		t.Fatalf("ListStorageQuotas with tenant: %v", err)
	}
	if len(quotas) != 1 {
		t.Errorf("expected 1 quota, got %d", len(quotas))
	}
}

// TestListAuditEvents_WithTenant tests ListAuditEvents with tenant filter.
func TestListAuditEvents_WithTenant(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]AuditEvent{{ID: "ae-1", Action: "create"}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	events, err := c.ListAuditEvents(context.Background(), "", "tenant-1")
	if err != nil {
		t.Fatalf("ListAuditEvents with tenant filter: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
}

// TestListDisks_WithNode tests ListDisks with node filter.
func TestListDisks_WithNode(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]DiskInfo{{ID: "disk-1"}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	disks, err := c.ListDisks(context.Background(), "node-1")
	if err != nil {
		t.Fatalf("ListDisks with node: %v", err)
	}
	if len(disks) != 1 {
		t.Fatalf("expected 1 disk, got %d", len(disks))
	}
}

// TestRunDoctorCheck_Success tests RunDoctorCheck with success response.
func TestRunDoctorCheck_Success(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]interface{}{"checks": []map[string]string{}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	resp, err := c.RunDoctorCheck(context.Background(), "all", map[string]string{})
	if err != nil {
		t.Fatalf("RunDoctorCheck: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("expected success response")
	}
}

// TestListIntegrations_Success tests ListIntegrations with success response.
func TestListIntegrations_Success(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal([]Integration{{Name: "test"}}),
	}
	srv := newTestServer(t, 200, payload)
	defer srv.Close()

	c := newTestClient(srv)
	integrations, err := c.ListIntegrations(context.Background())
	if err != nil {
		t.Fatalf("ListIntegrations: %v", err)
	}
	if len(integrations) != 1 {
		t.Fatalf("expected 1 integration, got %d", len(integrations))
	}
}

// TestClient_do_Unauthorized tests do() with 401 status.
func TestClient_do_Unauthorized(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"error":"unauthorized"}`))
	}))
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error for 401, got nil")
	}
	if apiErr, ok := err.(*APIError); ok && apiErr.Code != 3 {
		t.Errorf("expected exit code 3, got %d", apiErr.Code)
	}
}

// TestClient_do_Forbidden_TenantMismatch tests do() with 403 for tenant mismatch.
func TestClient_do_Forbidden_TenantMismatch(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"error":"tenant mismatch"}`))
	}))
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error for 403, got nil")
	}
	if apiErr, ok := err.(*APIError); ok && apiErr.Code != 4 {
		t.Errorf("expected exit code 4 for tenant mismatch, got %d", apiErr.Code)
	}
}

// TestClient_do_Forbidden_Scope tests do() with 403 for insufficient scope.
func TestClient_do_Forbidden_Scope(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"error":"insufficient scope"}`))
	}))
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error for 403, got nil")
	}
	if apiErr, ok := err.(*APIError); ok && apiErr.Code != 3 {
		t.Errorf("expected exit code 3 for insufficient scope, got %d", apiErr.Code)
	}
}

// TestClient_do_TooManyRequests tests do() with 429 status.
func TestClient_do_TooManyRequests(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"error":"rate limited"}`))
	}))
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error for 429, got nil")
	}
	if apiErr, ok := err.(*APIError); ok && apiErr.Code != 5 {
		t.Errorf("expected exit code 5 for rate limit, got %d", apiErr.Code)
	}
}

// TestClient_do_BadRequest_NonJSON tests do() with 400 and non-JSON body.
func TestClient_do_BadRequest_NonJSON(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte(`Bad request`))
	}))
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error for bad request, got nil")
	}
}

// TestClient_do_InvalidJSON tests do() with invalid JSON response.
func TestClient_do_InvalidJSON(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{invalid json`))
	}))
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error for invalid JSON, got nil")
	}
}

// TestClient_do_202Accepted tests do() with 202 Accepted status (deferred).
func TestClient_do_202Accepted(t *testing.T) {
	payload := APIResponse{
		Status: "deferred",
		Data: mustMarshal([]Node{{ID: "n1", Hostname: "node01", State: "pending"}}),
	}
	srv := newTestServer(t, http.StatusAccepted, payload)
	defer srv.Close()

	c := newTestClient(srv)
	nodes, err := c.ListNodes(context.Background(), NodeListParams{})
	if err != nil {
		t.Fatalf("do() with 202: %v", err)
	}
	if len(nodes) == 0 {
		t.Errorf("expected non-empty response from 202")
	}
}

// ===== Tests for new features (WithVerbose, WithTLSCAFile, WithInsecureSkipVerify, maskedToken, GetClientVersion, doWithRetry) =====

// TestNew_WithVerbose tests that New() with WithVerbose option enables debug logging.
func TestNew_WithVerbose(t *testing.T) {
	srv := newTestServer(t, http.StatusOK, APIResponse{
		Status: "success",
		Data:   mustMarshal([]Node{{ID: "n1", Hostname: "node01", State: "ready"}}),
	})
	defer srv.Close()

	// Create client with verbose enabled
	c := New(srv.URL, "test-token-verbose", WithVerbose(true))
	c.baseURL = srv.URL + "/api/v1"

	// Verify verbose flag is set
	if !c.verbose {
		t.Errorf("expected verbose=true, got false")
	}

	// Verify logger is set
	if c.logger == nil {
		t.Errorf("expected logger to be initialized, got nil")
	}

	// Make a request to ensure no panic occurs
	nodes, err := c.ListNodes(context.Background(), NodeListParams{})
	if err != nil {
		t.Fatalf("ListNodes with verbose: %v", err)
	}
	if len(nodes) != 1 {
		t.Errorf("expected 1 node, got %d", len(nodes))
	}
}

// TestNew_WithVerbose_DefaultBehavior tests New() without WithVerbose.
func TestNew_WithVerbose_DefaultBehavior(t *testing.T) {
	c := New("http://localhost:8080", "test-token")
	if c.verbose {
		t.Errorf("expected verbose=false by default, got true")
	}
	if c.logger != nil {
		t.Errorf("expected logger=nil by default, got non-nil")
	}
}

// TestNew_WithInsecureSkipVerify tests that New() with WithInsecureSkipVerify option configures TLS.
func TestNew_WithInsecureSkipVerify(t *testing.T) {
	c := New("https://localhost:8443", "test-token", WithInsecureSkipVerify(true))
	if !c.insecureSkipTLS {
		t.Errorf("expected insecureSkipTLS=true, got false")
	}
	if c.httpClient.Transport == nil {
		t.Errorf("expected transport to be configured, got nil")
	}
}

// TestNew_WithTLSCAFile_ValidPath tests that New() with WithTLSCAFile sets the CA file path.
func TestNew_WithTLSCAFile_ValidPath(t *testing.T) {
	// Write a minimal valid PEM-encoded certificate to a temp file
	certPEM := `-----BEGIN CERTIFICATE-----
MIICpDCCAYwCCQDU+pQ4pHgSpDANBgkqhkiG9w0BAQsFADAUMRIwEAYDVQQDDAls
b2NhbGhvc3QwHhcNMjMwMTAxMDAwMDAwWhcNMjQwMTAxMDAwMDAwWjAUMRIwEAYD
VQQDDAlsb2NhbGhvc3QwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQC7
o4qne60TB3pxwCZE0rqWKs3rNaR3P9QHBO8FRxJJsANfV7a3mLmNnmNVHXfwR9jG
cUKT6LUFIijf1o6W2lqKCEMqFZtFolm2bzC4OT9nCJQD2N0YD5IK1TKKQQD/0cCG
XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
-----END CERTIFICATE-----`
	tmpFile := t.TempDir() + "/ca.pem"
	if err := os.WriteFile(tmpFile, []byte(certPEM), 0644); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}

	c := New("https://localhost:8443", "test-token", WithTLSCAFile(tmpFile))
	if c.tlsCAFile != tmpFile {
		t.Errorf("expected tlsCAFile=%q, got %q", tmpFile, c.tlsCAFile)
	}
	if c.httpClient.Transport == nil {
		t.Errorf("expected transport to be configured, got nil")
	}
}

// TestNew_WithTLSCAFile_NonexistentFile tests New() with nonexistent CA file.
// The client construction succeeds, but the file won't be loaded (silently ignored).
func TestNew_WithTLSCAFile_NonexistentFile(t *testing.T) {
	nonexistentPath := "/nonexistent/ca.pem"
	c := New("https://localhost:8443", "test-token", WithTLSCAFile(nonexistentPath))
	if c.tlsCAFile != nonexistentPath {
		t.Errorf("expected tlsCAFile=%q, got %q", nonexistentPath, c.tlsCAFile)
	}
	// Construction should not error (failure to read the cert is silently ignored in New()).
	if c.httpClient.Transport == nil {
		t.Errorf("expected transport to be configured, got nil")
	}
}

// TestNew_MultipleOptions tests New() with multiple options combined.
func TestNew_MultipleOptions(t *testing.T) {
	tmpFile := t.TempDir() + "/ca.pem"
	_ = os.WriteFile(tmpFile, []byte("dummy"), 0644)

	c := New("https://localhost:8443", "test-token",
		WithVerbose(true),
		WithInsecureSkipVerify(true),
		WithTLSCAFile(tmpFile),
	)

	if !c.verbose {
		t.Errorf("expected verbose=true, got false")
	}
	if !c.insecureSkipTLS {
		t.Errorf("expected insecureSkipTLS=true, got false")
	}
	if c.tlsCAFile != tmpFile {
		t.Errorf("expected tlsCAFile=%q, got %q", tmpFile, c.tlsCAFile)
	}
	if c.logger == nil {
		t.Errorf("expected logger to be initialized, got nil")
	}
}

// TestMaskedToken tests the maskedToken() method.
func TestMaskedToken(t *testing.T) {
	tests := []struct {
		name     string
		token    string
		expected string
	}{
		{"long token", "sk_test_abcd1234efgh5678", "tok_****5678"},
		{"short token", "abc", "tok_****"},
		{"empty token", "", "tok_****"},
		{"exactly 4 chars", "1234", "tok_****1234"},
		{"5 chars", "12345", "tok_****2345"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c := &Client{token: tt.token}
			got := c.maskedToken()
			if got != tt.expected {
				t.Errorf("maskedToken()=%q; want %q", got, tt.expected)
			}
		})
	}
}

// TestMaskedToken_VerboseLogging verifies that maskedToken is called during verbose logging.
func TestMaskedToken_VerboseLogging(t *testing.T) {
	srv := newTestServer(t, http.StatusOK, APIResponse{
		Status: "success",
		Data:   mustMarshal([]Node{{ID: "n1", Hostname: "node01", State: "ready"}}),
	})
	defer srv.Close()

	c := New(srv.URL, "sk_test_1234567890ab", WithVerbose(true))
	c.baseURL = srv.URL + "/api/v1"

	// Make a request; maskedToken will be called internally by do().
	_, _ = c.ListNodes(context.Background(), NodeListParams{})
	// If no panic occurred, the test passes.
}

// TestGetClientVersion_Success tests GetClientVersion with a successful response.
func TestGetClientVersion_Success(t *testing.T) {
	payload := APIResponse{
		Status: "success",
		Data:   mustMarshal(map[string]string{"latest_version": "v1.2.3"}),
	}
	srv := newTestServer(t, http.StatusOK, payload)
	defer srv.Close()

	c := newTestClient(srv)
	resp, err := c.GetClientVersion(context.Background())
	if err != nil {
		t.Fatalf("GetClientVersion: %v", err)
	}
	if resp.Status != "success" {
		t.Errorf("status: got %q, want %q", resp.Status, "success")
	}

	var data map[string]string
	if err := DecodeData(resp, &data); err != nil {
		t.Fatalf("DecodeData: %v", err)
	}
	if data["latest_version"] != "v1.2.3" {
		t.Errorf("latest_version: got %q, want %q", data["latest_version"], "v1.2.3")
	}
}

// TestGetClientVersion_Error tests GetClientVersion with an error response.
func TestGetClientVersion_Error(t *testing.T) {
	srv := newTestServer(t, http.StatusServiceUnavailable, map[string]string{"error": "cluster unhealthy"})
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.GetClientVersion(context.Background())
	if err == nil {
		t.Fatal("expected error for 503, got nil")
	}
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.Code != 9 {
		t.Errorf("exit code: got %d, want 9", apiErr.Code)
	}
}

// TestRetry_429_WithRetryAfter tests doWithRetry with 429 followed by 200.
func TestRetry_429_WithRetryAfter(t *testing.T) {
	var callCount int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callCount++
		if callCount < 2 {
			// First request: return 429
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Retry-After", "0")
			w.WriteHeader(http.StatusTooManyRequests)
			_ = json.NewEncoder(w).Encode(map[string]string{"error": "rate limited"})
		} else {
			// Second request: return 200
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(APIResponse{
				Status: "success",
				Data:   mustMarshal([]Node{{ID: "n1", Hostname: "node01", State: "ready"}}),
			})
		}
	}))
	defer srv.Close()

	c := newTestClient(srv)
	nodes, err := c.ListNodes(context.Background(), NodeListParams{})
	if err != nil {
		t.Fatalf("ListNodes with 429 retry: %v", err)
	}
	if len(nodes) != 1 {
		t.Errorf("expected 1 node, got %d", len(nodes))
	}
	if callCount != 2 {
		t.Errorf("expected 2 calls (1 retry), got %d", callCount)
	}
}

// TestRetry_429_MaxRetriesExceeded tests doWithRetry when all retries are exhausted.
func TestRetry_429_MaxRetriesExceeded(t *testing.T) {
	var callCount int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callCount++
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Retry-After", "0")
		w.WriteHeader(http.StatusTooManyRequests)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "rate limited"})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error after max retries, got nil")
	}
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("expected *APIError, got %T", err)
	}
	if apiErr.Code != 5 {
		t.Errorf("exit code: got %d, want 5", apiErr.Code)
	}
	// Should have made 4 calls: 1 initial + 3 retries
	if callCount != 4 {
		t.Errorf("expected 4 calls (1 initial + 3 retries), got %d", callCount)
	}
}

// TestRetry_429_ContextCancelled tests doWithRetry with cancelled context.
func TestRetry_429_ContextCancelled(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Retry-After", "0")
		w.WriteHeader(http.StatusTooManyRequests)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "rate limited"})
	}))
	defer srv.Close()

	c := newTestClient(srv)

	// Create a context that's already cancelled
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := c.ListNodes(ctx, NodeListParams{})
	if err == nil {
		t.Fatal("expected error for cancelled context, got nil")
	}
	if !strings.Contains(err.Error(), "context canceled") {
		t.Errorf("expected 'context canceled' error, got %v", err)
	}
}

// TestRetry_NonRetryableError tests doWithRetry with non-retryable error (401).
func TestRetry_NonRetryableError(t *testing.T) {
	var callCount int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callCount++
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "unauthorized"})
	}))
	defer srv.Close()

	c := newTestClient(srv)
	_, err := c.ListNodes(context.Background(), NodeListParams{})
	if err == nil {
		t.Fatal("expected error for 401, got nil")
	}
	// Should have made only 1 call (no retries for non-429)
	if callCount != 1 {
		t.Errorf("expected 1 call (no retries), got %d", callCount)
	}
}
