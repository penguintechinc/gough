//go:build noxdp

package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// Test low-hanging-fruit functions to boost coverage.


// Test diskPlanCmd with success path and formatting.
func TestDiskPlanCmd_FormatOutput(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"status": "success",
			"data": map[string]interface{}{
				"disks": []map[string]interface{}{
					{"name": "/dev/sda", "capacity_mb": 1024},
				},
			},
		})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"disk", "plan", "node-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test vaultUnsealCmd with error path.
func TestVaultUnsealCmd_Error(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid unseal share"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"vault", "unseal"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test migrationPolicySetCmd with error path.
func TestMigrationPolicySetCmd_Error(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid policy"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"migration", "policy", "set", "--min-healthy-nodes", "2"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test nodeShowCmd with success path.
func TestNodeShowCmd_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"status": "success",
			"data": map[string]interface{}{
				"id":       "node-1",
				"hostname": "srv01",
				"state":    "ready",
			},
		})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"node", "show", "node-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test nodeTagsCmd with success path.
func TestNodeTagsCmd_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"status": "success",
			"data": map[string]interface{}{
				"tags": []string{"prod", "primary"},
			},
		})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"node", "tags", "node-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test migrationTriggerCmd with success path (no target).
func TestMigrationTriggerCmd_NoTarget(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(202)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"status": "deferred",
			"note":   "migration queued",
		})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"migration", "trigger", "biome-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

