//go:build noxdp

package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// Test error paths for commands with low coverage.
// These tests ensure that error handling branches in RunE methods are covered.

// Test biomeEligibilityCheckCmd with a generic API error.
func TestBiomeEligibilityCheckCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid request"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"biome", "eligibility-check", "instance-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test clusterAdoptCmd with a generic API error.
func TestClusterAdoptCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid request"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"cluster", "adopt", "k8s"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test biomeValidateCmd with a generic API error.
func TestBiomeValidateCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "validation failed"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"biome", "validate"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test clusterIdentityPlaneStatusCmd with an API error.
func TestClusterIdentityPlaneStatusCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(503)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "service unavailable"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"cluster", "identity-plane", "status"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test clusterNetworkBaselineConfigureCmd with an API error.
func TestClusterNetworkBaselineConfigureCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid network"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"cluster", "network-baseline", "configure", "192.168.1.0/24"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test clusterRotateJoinerSecretsCmd with an API error.
func TestClusterRotateJoinerSecretsCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(503)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "cluster unhealthy"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"cluster", "rotate-joiner-secrets"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test clusterIdentityPlaneConfigureCmd with an API error.
func TestClusterIdentityPlaneConfigureCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid config"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"cluster", "identity-plane", "configure", "skauswatch", "https://skauswatch.local"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test auditExportCmd with an API error.
func TestAuditExportCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid filter"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"audit", "export", "/tmp/audit.log"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test capacityForecastCmd with an API error.
func TestCapacityForecastCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(503)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "service unavailable"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"capacity", "forecast"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test configSetCmd with an API error (for set operation).
func TestConfigSetCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid config"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"config", "set", "key", "value"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test deploymentCancelCmd with an API error.
func TestDeploymentCancelCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "deployment not found"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"deployment", "cancel", "dep-1"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test clusterUpgradeCmd with an API error.
func TestClusterUpgradeCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(503)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "cluster unhealthy"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"cluster", "upgrade", "v1.0.0"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}

// Test deploymentLogsCmd with an API error.
func TestDeploymentLogsCmd_APIError(t *testing.T) {
	_ = mockExit(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "deployment not found"})
	}))
	defer srv.Close()

	t.Setenv("GOUGH_TOKEN", "test-token")
	t.Setenv("GOUGH_CLUSTER_URL", srv.URL)
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	rootCmd.SetArgs([]string{"deployment", "logs"})
	var outBuf bytes.Buffer
	rootCmd.SetOut(&outBuf)
	_ = rootCmd.Execute()
}
