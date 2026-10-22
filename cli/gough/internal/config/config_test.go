//go:build noxdp

package config

import (
	"os"
	"path/filepath"
	"testing"
	"runtime"
)

func setupTestConfig(t *testing.T) (*Config, string) {
	t.Helper()
	tmpDir := t.TempDir()
	oldConfigHome := os.Getenv("XDG_CONFIG_HOME")
	oldAppData := os.Getenv("APPDATA")
	oldHome := os.Getenv("HOME")

	t.Cleanup(func() {
		if oldConfigHome == "" {
			os.Unsetenv("XDG_CONFIG_HOME")
		} else {
			os.Setenv("XDG_CONFIG_HOME", oldConfigHome)
		}
		if oldAppData == "" {
			os.Unsetenv("APPDATA")
		} else {
			os.Setenv("APPDATA", oldAppData)
		}
		if oldHome == "" {
			os.Unsetenv("HOME")
		} else {
			os.Setenv("HOME", oldHome)
		}
	})

	os.Setenv("XDG_CONFIG_HOME", tmpDir)
	os.Setenv("HOME", tmpDir)
	cfg, _ := New()
	return cfg, tmpDir
}

func TestNew(t *testing.T) {
	cfg, _ := setupTestConfig(t)
	if cfg == nil {
		t.Fatal("New() returned nil config")
	}
}

func TestNewWithBadConfigHome(t *testing.T) {
	oldConfigHome := os.Getenv("XDG_CONFIG_HOME")
	oldHome := os.Getenv("HOME")
	t.Cleanup(func() {
		if oldConfigHome == "" {
			os.Unsetenv("XDG_CONFIG_HOME")
		} else {
			os.Setenv("XDG_CONFIG_HOME", oldConfigHome)
		}
		if oldHome == "" {
			os.Unsetenv("HOME")
		} else {
			os.Setenv("HOME", oldHome)
		}
	})

	os.Unsetenv("XDG_CONFIG_HOME")
	os.Unsetenv("HOME")

	_, err := New()
	if err == nil {
		t.Fatal("New() expected error when config dir cannot be determined")
	}
}

func TestGet(t *testing.T) {
	cfg, _ := setupTestConfig(t)
	val := cfg.Get("nonexistent.key")
	if val != "" {
		t.Errorf("Get() expected empty string, got %q", val)
	}
}

func TestSet(t *testing.T) {
	cfg, tmpDir := setupTestConfig(t)
	err := cfg.Set("test.key", "test_value")
	if err != nil {
		t.Fatalf("Set() failed: %v", err)
	}

	val := cfg.Get("test.key")
	if val != "test_value" {
		t.Errorf("Get() expected 'test_value', got %q", val)
	}

	expectedPath := filepath.Join(tmpDir, appName, configFileName+".yaml")
	if _, err := os.Stat(expectedPath); err != nil {
		t.Errorf("config file not created at %q: %v", expectedPath, err)
	}
}

func TestSetCreatesMissingDir(t *testing.T) {
	cfg, tmpDir := setupTestConfig(t)
	configDir := filepath.Join(tmpDir, appName)

	// Remove the config dir to test creation
	os.RemoveAll(configDir)

	err := cfg.Set("test.key", "test_value")
	if err != nil {
		t.Fatalf("Set() failed: %v", err)
	}

	expectedPath := filepath.Join(tmpDir, appName, configFileName+".yaml")
	if _, err := os.Stat(expectedPath); err != nil {
		t.Errorf("config file not created at %q: %v", expectedPath, err)
	}
}

func TestUnset(t *testing.T) {
	cfg, _ := setupTestConfig(t)
	cfg.Set("test.key", "test_value")
	err := cfg.Unset("test.key")
	if err != nil {
		t.Fatalf("Unset() failed: %v", err)
	}

	val := cfg.Get("test.key")
	if val != "" {
		t.Errorf("Get() expected empty string after unset, got %q", val)
	}
}

func TestAll(t *testing.T) {
	cfg, _ := setupTestConfig(t)
	cfg.Set("key1", "value1")
	cfg.Set("key2", "value2")

	all := cfg.All()
	if len(all) == 0 {
		t.Fatal("All() returned empty map")
	}
	if all["key1"] != "value1" {
		t.Errorf("All() expected key1='value1', got %v", all["key1"])
	}
	if all["key2"] != "value2" {
		t.Errorf("All() expected key2='value2', got %v", all["key2"])
	}
}

func TestClusterURL(t *testing.T) {
	cfg, _ := setupTestConfig(t)
	url := cfg.ClusterURL()
	if url != "" {
		t.Errorf("ClusterURL() expected empty string, got %q", url)
	}
}

func TestSetClusterURL(t *testing.T) {
	cfg, _ := setupTestConfig(t)
	testURL := "https://example.com:8080"
	err := cfg.SetClusterURL(testURL)
	if err != nil {
		t.Fatalf("SetClusterURL() failed: %v", err)
	}

	url := cfg.ClusterURL()
	if url != testURL {
		t.Errorf("ClusterURL() expected %q, got %q", testURL, url)
	}
}

func TestCurrentContext(t *testing.T) {
	cfg, _ := setupTestConfig(t)
	ctx := cfg.CurrentContext()
	if ctx != "" {
		t.Errorf("CurrentContext() expected empty string, got %q", ctx)
	}
}

func TestPath(t *testing.T) {
	_, tmpDir := setupTestConfig(t)
	oldConfigHome := os.Getenv("XDG_CONFIG_HOME")
	t.Cleanup(func() {
		if oldConfigHome == "" {
			os.Unsetenv("XDG_CONFIG_HOME")
		} else {
			os.Setenv("XDG_CONFIG_HOME", oldConfigHome)
		}
	})
	os.Setenv("XDG_CONFIG_HOME", tmpDir)

	path, err := Path()
	if err != nil {
		t.Fatalf("Path() failed: %v", err)
	}
	if path == "" {
		t.Fatal("Path() returned empty string")
	}
	if !filepath.IsAbs(path) {
		t.Errorf("Path() expected absolute path, got %q", path)
	}
}

func TestPathWithoutConfigHome(t *testing.T) {
	tmpDir := t.TempDir()
	oldConfigHome := os.Getenv("XDG_CONFIG_HOME")
	oldHome := os.Getenv("HOME")
	t.Cleanup(func() {
		if oldConfigHome == "" {
			os.Unsetenv("XDG_CONFIG_HOME")
		} else {
			os.Setenv("XDG_CONFIG_HOME", oldConfigHome)
		}
		if oldHome == "" {
			os.Unsetenv("HOME")
		} else {
			os.Setenv("HOME", oldHome)
		}
	})

	os.Unsetenv("XDG_CONFIG_HOME")
	os.Setenv("HOME", tmpDir)

	path, err := Path()
	if err != nil {
		t.Fatalf("Path() failed: %v", err)
	}
	if path == "" {
		t.Fatal("Path() returned empty string")
	}
}

func TestEnvVarOverride(t *testing.T) {
	cfg, _ := setupTestConfig(t)
	oldVal := os.Getenv("GOUGH_TEST_KEY")
	t.Cleanup(func() {
		if oldVal == "" {
			os.Unsetenv("GOUGH_TEST_KEY")
		} else {
			os.Setenv("GOUGH_TEST_KEY", oldVal)
		}
	})
	os.Setenv("GOUGH_TEST_KEY", "env_value")

	val := cfg.Get("test.key")
	if val != "env_value" {
		t.Errorf("Get() expected 'env_value' from env var, got %q", val)
	}
}

func TestConfigDirWindows(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("Windows-only test")
	}

	oldAppData := os.Getenv("APPDATA")
	oldHome := os.Getenv("HOME")
	t.Cleanup(func() {
		if oldAppData == "" {
			os.Unsetenv("APPDATA")
		} else {
			os.Setenv("APPDATA", oldAppData)
		}
		if oldHome == "" {
			os.Unsetenv("HOME")
		} else {
			os.Setenv("HOME", oldHome)
		}
	})

	tmpDir := t.TempDir()
	os.Setenv("APPDATA", tmpDir)
	os.Setenv("HOME", tmpDir)

	dir, err := configDir()
	if err != nil {
		t.Fatalf("configDir() failed: %v", err)
	}
	if dir == "" {
		t.Fatal("configDir() returned empty string")
	}
}

func TestConfigDirLinux(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix-only test")
	}

	tmpDir := t.TempDir()
	oldConfigHome := os.Getenv("XDG_CONFIG_HOME")
	oldHome := os.Getenv("HOME")
	t.Cleanup(func() {
		if oldConfigHome == "" {
			os.Unsetenv("XDG_CONFIG_HOME")
		} else {
			os.Setenv("XDG_CONFIG_HOME", oldConfigHome)
		}
		if oldHome == "" {
			os.Unsetenv("HOME")
		} else {
			os.Setenv("HOME", oldHome)
		}
	})

	os.Setenv("XDG_CONFIG_HOME", tmpDir)
	os.Setenv("HOME", tmpDir)

	dir, err := configDir()
	if err != nil {
		t.Fatalf("configDir() failed: %v", err)
	}
	if dir != filepath.Join(tmpDir, appName) {
		t.Errorf("configDir() expected %s, got %s", filepath.Join(tmpDir, appName), dir)
	}
}

func TestSetWithCreateDirError(t *testing.T) {
	cfg, _ := setupTestConfig(t)

	// Create a file where the config dir should be, so MkdirAll will fail
	tmpDir := t.TempDir()
	oldConfigHome := os.Getenv("XDG_CONFIG_HOME")
	t.Cleanup(func() {
		if oldConfigHome == "" {
			os.Unsetenv("XDG_CONFIG_HOME")
		} else {
			os.Setenv("XDG_CONFIG_HOME", oldConfigHome)
		}
	})

	os.Setenv("XDG_CONFIG_HOME", tmpDir)
	configPath := filepath.Join(tmpDir, appName)

	// Create a file with the name that should be a directory
	f, _ := os.Create(configPath)
	f.Close()

	cfg, _ = New()
	err := cfg.Set("test.key", "test_value")
	if err == nil {
		t.Fatal("Set() expected error when directory cannot be created")
	}
}

func TestConfigDirWithBadHome(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix-only test")
	}

	// Configure environment such that os.UserHomeDir() will be called
	oldConfigHome := os.Getenv("XDG_CONFIG_HOME")
	oldHome := os.Getenv("HOME")
	t.Cleanup(func() {
		if oldConfigHome == "" {
			os.Unsetenv("XDG_CONFIG_HOME")
		} else {
			os.Setenv("XDG_CONFIG_HOME", oldConfigHome)
		}
		if oldHome == "" {
			os.Unsetenv("HOME")
		} else {
			os.Setenv("HOME", oldHome)
		}
	})

	os.Unsetenv("XDG_CONFIG_HOME")
	os.Unsetenv("HOME")

	// This will trigger the error path in configDir()
	_, err := configDir()
	if err == nil {
		t.Fatal("configDir() expected error when HOME is not set")
	}
}

func TestMultipleKeysInConfig(t *testing.T) {
	cfg, _ := setupTestConfig(t)

	// Set multiple keys
	cfg.Set("db.host", "localhost")
	cfg.Set("db.port", "5432")

	if cfg.Get("db.host") != "localhost" {
		t.Errorf("expected db.host=localhost, got %q", cfg.Get("db.host"))
	}
	if cfg.Get("db.port") != "5432" {
		t.Errorf("expected db.port=5432, got %q", cfg.Get("db.port"))
	}
}

func TestSetAndGetContext(t *testing.T) {
	cfg, _ := setupTestConfig(t)

	// SetContext with valid data
	ctx := Context{
		ClusterURL: "https://prod.example.com",
		TenantID:   "tenant-123",
	}
	err := cfg.SetContext("prod", ctx)
	if err != nil {
		t.Fatalf("SetContext() failed: %v", err)
	}

	// GetContext should return the same data
	retrieved, exists := cfg.GetContext("prod")
	if !exists {
		t.Fatal("GetContext() returned false for existing context")
	}
	if retrieved.ClusterURL != ctx.ClusterURL {
		t.Errorf("ClusterURL mismatch: expected %q, got %q", ctx.ClusterURL, retrieved.ClusterURL)
	}
	if retrieved.TenantID != ctx.TenantID {
		t.Errorf("TenantID mismatch: expected %q, got %q", ctx.TenantID, retrieved.TenantID)
	}

	// GetContext for nonexistent context should return false
	_, exists = cfg.GetContext("nonexistent")
	if exists {
		t.Fatal("GetContext() returned true for nonexistent context")
	}
}

func TestContexts_MultipleContexts(t *testing.T) {
	cfg, _ := setupTestConfig(t)

	// Set multiple contexts
	prodCtx := Context{
		ClusterURL: "https://prod.example.com",
		TenantID:   "tenant-prod",
	}
	stagingCtx := Context{
		ClusterURL: "https://staging.example.com",
		TenantID:   "tenant-staging",
	}

	err := cfg.SetContext("prod", prodCtx)
	if err != nil {
		t.Fatalf("SetContext(prod) failed: %v", err)
	}
	err = cfg.SetContext("staging", stagingCtx)
	if err != nil {
		t.Fatalf("SetContext(staging) failed: %v", err)
	}

	// Contexts() should return both
	contexts := cfg.Contexts()
	if len(contexts) != 2 {
		t.Errorf("Contexts() expected 2 contexts, got %d", len(contexts))
	}

	// Verify both exist and have correct values
	if prodRetrieved, ok := contexts["prod"]; !ok {
		t.Error("Contexts() missing 'prod' key")
	} else {
		if prodRetrieved.ClusterURL != prodCtx.ClusterURL {
			t.Errorf("prod ClusterURL: expected %q, got %q", prodCtx.ClusterURL, prodRetrieved.ClusterURL)
		}
		if prodRetrieved.TenantID != prodCtx.TenantID {
			t.Errorf("prod TenantID: expected %q, got %q", prodCtx.TenantID, prodRetrieved.TenantID)
		}
	}

	if stagingRetrieved, ok := contexts["staging"]; !ok {
		t.Error("Contexts() missing 'staging' key")
	} else {
		if stagingRetrieved.ClusterURL != stagingCtx.ClusterURL {
			t.Errorf("staging ClusterURL: expected %q, got %q", stagingCtx.ClusterURL, stagingRetrieved.ClusterURL)
		}
		if stagingRetrieved.TenantID != stagingCtx.TenantID {
			t.Errorf("staging TenantID: expected %q, got %q", stagingCtx.TenantID, stagingRetrieved.TenantID)
		}
	}
}

func TestDeleteContext(t *testing.T) {
	cfg, _ := setupTestConfig(t)

	// Set a context
	ctx := Context{
		ClusterURL: "https://dev.example.com",
		TenantID:   "tenant-dev",
	}
	err := cfg.SetContext("dev", ctx)
	if err != nil {
		t.Fatalf("SetContext() failed: %v", err)
	}

	// Verify it exists
	_, exists := cfg.GetContext("dev")
	if !exists {
		t.Fatal("GetContext() failed after SetContext()")
	}

	// Delete the context
	err = cfg.DeleteContext("dev")
	if err != nil {
		t.Fatalf("DeleteContext() failed: %v", err)
	}

	// Verify it no longer exists
	_, exists = cfg.GetContext("dev")
	if exists {
		t.Fatal("GetContext() returned true after DeleteContext()")
	}

	// DeleteContext on nonexistent context should return error
	err = cfg.DeleteContext("nonexistent")
	if err == nil {
		t.Fatal("DeleteContext() expected error for nonexistent context")
	}
}

func TestUseContext(t *testing.T) {
	cfg, _ := setupTestConfig(t)

	// Set contexts
	prodCtx := Context{
		ClusterURL: "https://prod.example.com",
		TenantID:   "tenant-prod",
	}
	stagingCtx := Context{
		ClusterURL: "https://staging.example.com",
		TenantID:   "tenant-staging",
	}

	cfg.SetContext("prod", prodCtx)
	cfg.SetContext("staging", stagingCtx)

	// UseContext should set the active context
	err := cfg.UseContext("prod")
	if err != nil {
		t.Fatalf("UseContext(prod) failed: %v", err)
	}

	current := cfg.CurrentContext()
	if current != "prod" {
		t.Errorf("CurrentContext() expected 'prod', got %q", current)
	}

	// Switch to staging
	err = cfg.UseContext("staging")
	if err != nil {
		t.Fatalf("UseContext(staging) failed: %v", err)
	}

	current = cfg.CurrentContext()
	if current != "staging" {
		t.Errorf("CurrentContext() expected 'staging', got %q", current)
	}

	// UseContext with nonexistent context should return error
	err = cfg.UseContext("nonexistent")
	if err == nil {
		t.Fatal("UseContext() expected error for nonexistent context")
	}

	// CurrentContext should still be "staging"
	current = cfg.CurrentContext()
	if current != "staging" {
		t.Errorf("CurrentContext() expected 'staging' after failed UseContext, got %q", current)
	}
}

func TestActiveContext(t *testing.T) {
	cfg, _ := setupTestConfig(t)

	// When no context is set, ActiveContext should return false
	name, _, exists := cfg.ActiveContext()
	if exists {
		t.Fatal("ActiveContext() should return false when no context is set")
	}
	if name != "" {
		t.Errorf("ActiveContext() name should be empty, got %q", name)
	}

	// Set and use a context
	ctx := Context{
		ClusterURL: "https://example.com",
		TenantID:   "tenant-abc",
	}
	cfg.SetContext("test", ctx)
	cfg.UseContext("test")

	// ActiveContext should now return the context
	name, activeCtx, exists := cfg.ActiveContext()
	if !exists {
		t.Fatal("ActiveContext() returned false after setting active context")
	}
	if name != "test" {
		t.Errorf("ActiveContext() name expected 'test', got %q", name)
	}
	if activeCtx.ClusterURL != ctx.ClusterURL {
		t.Errorf("ActiveContext() ClusterURL expected %q, got %q", ctx.ClusterURL, activeCtx.ClusterURL)
	}
	if activeCtx.TenantID != ctx.TenantID {
		t.Errorf("ActiveContext() TenantID expected %q, got %q", ctx.TenantID, activeCtx.TenantID)
	}

	// Delete the active context and verify ActiveContext returns false
	cfg.DeleteContext("test")
	name, _, exists = cfg.ActiveContext()
	if exists {
		t.Fatal("ActiveContext() should return false after deleting the active context")
	}
}

func TestSetContext_Persistence(t *testing.T) {
	cfg1, tmpDir := setupTestConfig(t)

	// Set a context with the first config instance
	ctx := Context{
		ClusterURL: "https://persistent.example.com",
		TenantID:   "tenant-persist",
	}
	err := cfg1.SetContext("persistent", ctx)
	if err != nil {
		t.Fatalf("SetContext() failed: %v", err)
	}

	// Create a new config instance with the same directory
	oldConfigHome := os.Getenv("XDG_CONFIG_HOME")
	t.Cleanup(func() {
		if oldConfigHome == "" {
			os.Unsetenv("XDG_CONFIG_HOME")
		} else {
			os.Setenv("XDG_CONFIG_HOME", oldConfigHome)
		}
	})
	os.Setenv("XDG_CONFIG_HOME", tmpDir)

	cfg2, err := New()
	if err != nil {
		t.Fatalf("New() failed: %v", err)
	}

	// Verify the context persisted
	retrieved, exists := cfg2.GetContext("persistent")
	if !exists {
		t.Fatal("GetContext() returned false for persisted context")
	}
	if retrieved.ClusterURL != ctx.ClusterURL {
		t.Errorf("ClusterURL mismatch: expected %q, got %q", ctx.ClusterURL, retrieved.ClusterURL)
	}
	if retrieved.TenantID != ctx.TenantID {
		t.Errorf("TenantID mismatch: expected %q, got %q", ctx.TenantID, retrieved.TenantID)
	}
}

func TestContexts_EmptyWhenNone(t *testing.T) {
	cfg, _ := setupTestConfig(t)

	// Contexts should return empty map when none are set
	contexts := cfg.Contexts()
	if len(contexts) != 0 {
		t.Errorf("Contexts() expected empty map, got %d contexts", len(contexts))
	}
}

func TestSetContext_OverwriteExisting(t *testing.T) {
	cfg, _ := setupTestConfig(t)

	// Set initial context
	ctx1 := Context{
		ClusterURL: "https://old.example.com",
		TenantID:   "tenant-old",
	}
	err := cfg.SetContext("test", ctx1)
	if err != nil {
		t.Fatalf("first SetContext() failed: %v", err)
	}

	// Overwrite with new context
	ctx2 := Context{
		ClusterURL: "https://new.example.com",
		TenantID:   "tenant-new",
	}
	err = cfg.SetContext("test", ctx2)
	if err != nil {
		t.Fatalf("second SetContext() failed: %v", err)
	}

	// GetContext should return the new values
	retrieved, exists := cfg.GetContext("test")
	if !exists {
		t.Fatal("GetContext() returned false after overwrite")
	}
	if retrieved.ClusterURL != ctx2.ClusterURL {
		t.Errorf("ClusterURL expected %q, got %q", ctx2.ClusterURL, retrieved.ClusterURL)
	}
	if retrieved.TenantID != ctx2.TenantID {
		t.Errorf("TenantID expected %q, got %q", ctx2.TenantID, retrieved.TenantID)
	}
}

// TestNew_MalformedYAML covers the ReadInConfig non-FileNotFound error path.
func TestNew_MalformedYAML(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	configDir := filepath.Join(dir, appName)
	if err := os.MkdirAll(configDir, 0o700); err != nil {
		t.Fatalf("MkdirAll: %v", err)
	}
	// Write syntactically invalid YAML so ReadInConfig returns a non-FileNotFound error.
	if err := os.WriteFile(filepath.Join(configDir, configFileName+".yaml"), []byte("{: invalid yaml ]["), 0o600); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}

	_, err := New()
	if err == nil {
		t.Fatal("expected error for malformed config file, got nil")
	}
}

// TestContexts_ScalarContextEntry covers the !ok continue branch in Contexts().
// A YAML config where a context value is a scalar (not a map) is simply skipped.
func TestContexts_ScalarContextEntry(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	configDir := filepath.Join(dir, appName)
	if err := os.MkdirAll(configDir, 0o700); err != nil {
		t.Fatalf("MkdirAll: %v", err)
	}
	// Write a config where one context entry is a scalar string, not a map.
	yaml := "contexts:\n  bad-ctx: \"not-a-map\"\n"
	if err := os.WriteFile(filepath.Join(configDir, configFileName+".yaml"), []byte(yaml), 0o600); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}

	cfg, err := New()
	if err != nil {
		t.Fatalf("New() failed: %v", err)
	}
	contexts := cfg.Contexts()
	if len(contexts) != 0 {
		// The bad entry should be silently skipped.
		t.Errorf("expected 0 valid contexts, got %d", len(contexts))
	}
}

// TestConfigDir_Windows exercises the Windows branch in configDir via runtime.GOOS check.
// Skipped on non-Windows platforms since it requires APPDATA env behaviour.
func TestConfigDir_WindowsAPPDATA(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("Windows-only test")
	}

	t.Setenv("APPDATA", `C:\Users\test\AppData\Roaming`)
	dir, err := configDir()
	if err != nil {
		t.Fatalf("configDir(): %v", err)
	}
	if dir == "" {
		t.Error("expected non-empty config dir")
	}
}

// TestPath_ConfigDirError covers the configDir() error return in Path() on non-Windows.
// Clearing both XDG_CONFIG_HOME and HOME causes os.UserHomeDir() to fail.
func TestPath_ConfigDirError(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows has separate APPDATA fallback — HOME trick does not apply")
	}
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("HOME", "")

	_, err := Path()
	if err == nil {
		t.Fatal("expected error when HOME is unset")
	}
}

// TestSave_ConfigDirError covers the configDir() error return in save() on non-Windows.
// The Config is created while XDG_CONFIG_HOME is valid; then env vars are cleared so
// the subsequent save() call inside Set() cannot resolve the config directory.
func TestSave_ConfigDirError(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows has separate APPDATA fallback — HOME trick does not apply")
	}

	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	cfg, err := New()
	if err != nil {
		t.Fatalf("New(): %v", err)
	}

	// Clear env vars so configDir() fails on the next save() call.
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("HOME", "")

	err = cfg.Set("key", "value")
	if err == nil {
		t.Fatal("expected error when HOME is unset")
	}
}
