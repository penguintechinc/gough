//go:build noxdp

// Package config manages CLI configuration persistence.
// Configuration is stored in $XDG_CONFIG_HOME/gough/config.yaml (or
// ~/.config/gough/config.yaml on Linux/macOS, %APPDATA%\gough\config.yaml on Windows).
package config

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/spf13/viper"
)

const (
	appName        = "gough"
	configFileName = "config"
)

// Context holds the connection settings for a named cluster context.
type Context struct {
	ClusterURL string
	TenantID   string
}

// Config holds all persistent CLI settings.
type Config struct {
	v *viper.Viper
}

// New returns a Config backed by the user's config file.
// If no config file exists the config is empty (not an error).
func New() (*Config, error) {
	v := viper.New()
	v.SetConfigName(configFileName)
	v.SetConfigType("yaml")

	dir, err := configDir()
	if err != nil {
		return nil, fmt.Errorf("config dir: %w", err)
	}
	v.AddConfigPath(dir)

	// Env-var override: GOUGH_<KEY> (uppercase, dots → underscores).
	v.SetEnvPrefix("GOUGH")
	v.SetEnvKeyReplacer(strings.NewReplacer(".", "_"))
	v.AutomaticEnv()

	if err := v.ReadInConfig(); err != nil {
		if _, ok := err.(viper.ConfigFileNotFoundError); !ok {
			return nil, fmt.Errorf("read config: %w", err)
		}
	}

	return &Config{v: v}, nil
}

// Get returns a config value as a string.
func (c *Config) Get(key string) string {
	return c.v.GetString(key)
}

// Set writes a config value and persists to disk.
func (c *Config) Set(key, value string) error {
	c.v.Set(key, value)
	return c.save()
}

// Unset removes a key from the config and persists.
func (c *Config) Unset(key string) error {
	c.v.Set(key, nil)
	return c.save()
}

// All returns a flat map of all config keys → values.
func (c *Config) All() map[string]interface{} {
	return c.v.AllSettings()
}

// ClusterURL is a convenience accessor for the active cluster URL.
func (c *Config) ClusterURL() string {
	return c.v.GetString("cluster.url")
}

// SetClusterURL is a convenience setter.
func (c *Config) SetClusterURL(url string) error {
	return c.Set("cluster.url", url)
}

// CurrentContext returns the active context name.
func (c *Config) CurrentContext() string {
	return c.v.GetString("current_context")
}

// GetContext returns the named context, or false if it does not exist.
func (c *Config) GetContext(name string) (Context, bool) {
	clusterURL := c.v.GetString("contexts." + name + ".cluster_url")
	tenantID := c.v.GetString("contexts." + name + ".tenant_id")
	if clusterURL == "" {
		return Context{}, false
	}
	return Context{
		ClusterURL: clusterURL,
		TenantID:   tenantID,
	}, true
}

// SetContext creates or updates a named context and persists config.
func (c *Config) SetContext(name string, ctx Context) error {
	c.v.Set("contexts."+name+".cluster_url", ctx.ClusterURL)
	c.v.Set("contexts."+name+".tenant_id", ctx.TenantID)
	return c.save()
}

// DeleteContext removes a named context and persists config.
func (c *Config) DeleteContext(name string) error {
	// Get the full contexts map and remove the entry
	contexts := c.v.GetStringMap("contexts")
	if _, exists := contexts[name]; !exists {
		return fmt.Errorf("context %q does not exist", name)
	}
	delete(contexts, name)
	c.v.Set("contexts", contexts)
	return c.save()
}

// Contexts returns all defined contexts.
func (c *Config) Contexts() map[string]Context {
	result := make(map[string]Context)
	contextsMap := c.v.GetStringMap("contexts")
	for name, val := range contextsMap {
		ctxMap, ok := val.(map[string]interface{})
		if !ok {
			continue
		}
		clusterURL, _ := ctxMap["cluster_url"].(string)
		tenantID, _ := ctxMap["tenant_id"].(string)
		result[name] = Context{
			ClusterURL: clusterURL,
			TenantID:   tenantID,
		}
	}
	return result
}

// UseContext sets the active context by name and persists config.
// Returns an error if the named context does not exist.
func (c *Config) UseContext(name string) error {
	_, exists := c.GetContext(name)
	if !exists {
		return fmt.Errorf("context %q not found", name)
	}
	c.v.Set("current_context", name)
	return c.save()
}

// ActiveContext returns the Context for the current_context setting.
// Returns zero Context and false if current_context is not set or not found.
func (c *Config) ActiveContext() (string, Context, bool) {
	name := c.CurrentContext()
	if name == "" {
		return "", Context{}, false
	}
	ctx, exists := c.GetContext(name)
	if !exists {
		return "", Context{}, false
	}
	return name, ctx, true
}

func (c *Config) save() error {
	dir, err := configDir()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}
	path := filepath.Join(dir, configFileName+".yaml")
	return c.v.WriteConfigAs(path)
}

// configDir returns the platform-appropriate config directory.
func configDir() (string, error) {
	switch runtime.GOOS {
	case "windows":
		base := os.Getenv("APPDATA")
		if base == "" {
			home, err := os.UserHomeDir()
			if err != nil {
				return "", err
			}
			base = filepath.Join(home, "AppData", "Roaming")
		}
		return filepath.Join(base, appName), nil
	default:
		// Linux / macOS: XDG_CONFIG_HOME or ~/.config
		base := os.Getenv("XDG_CONFIG_HOME")
		if base == "" {
			home, err := os.UserHomeDir()
			if err != nil {
				return "", err
			}
			base = filepath.Join(home, ".config")
		}
		return filepath.Join(base, appName), nil
	}
}

// Path returns the full path to the config file (may not yet exist).
func Path() (string, error) {
	dir, err := configDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, configFileName+".yaml"), nil
}
