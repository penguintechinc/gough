//go:build noxdp

// Package auth implements OIDC device-code flow login and OS keychain token management.
// Tokens are NEVER passed as CLI arguments or written to plaintext files.
// Tokens are masked in logs as tok_****<last4>.
package auth

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/zalando/go-keyring"
)

const (
	keychainService = "gough-cli"
	keychainUser    = "token"
	// GOUGH_TOKEN env var overrides keychain lookup.
	envVarToken = "GOUGH_TOKEN"
)

// TokenSet holds the access + refresh tokens returned by the OIDC provider.
type TokenSet struct {
	AccessToken  string    `json:"access_token"`
	RefreshToken string    `json:"refresh_token"`
	TokenType    string    `json:"token_type"`
	ExpiresAt    time.Time `json:"expires_at"`
	Scope        string    `json:"scope"`
	ClusterURL   string    `json:"cluster_url"`
}

// DeviceCodeResponse is the RFC 8628 device-authorization response.
type DeviceCodeResponse struct {
	DeviceCode              string `json:"device_code"`
	UserCode                string `json:"user_code"`
	VerificationURI         string `json:"verification_uri"`
	VerificationURIComplete string `json:"verification_uri_complete"`
	ExpiresIn               int    `json:"expires_in"`
	Interval                int    `json:"interval"`
}

// tokenPayload is the JSON object persisted to the keychain.
type tokenPayload struct {
	AccessToken  string    `json:"access_token"`
	RefreshToken string    `json:"refresh_token"`
	ExpiresAt    time.Time `json:"expires_at"`
	ClusterURL   string    `json:"cluster_url"`
}

// DeviceLogin performs OIDC device-code flow against the Gough cluster's OIDC
// discovery endpoint.  It prints the user-code and verification URL to stdout,
// then polls until the user completes authorization or the device code expires.
//
// On success the token set is stored in the OS keychain and returned.
func DeviceLogin(ctx context.Context, clusterURL string) (*TokenSet, error) {
	// Resolve OIDC endpoints from the Gough well-known endpoint.
	dc, tokenURL, err := startDeviceFlow(ctx, clusterURL)
	if err != nil {
		return nil, fmt.Errorf("start device flow: %w", err)
	}

	fmt.Printf("\n  Open the following URL in your browser:\n\n")
	fmt.Printf("  %s\n\n", dc.VerificationURIComplete)
	fmt.Printf("  Verification code: %s\n\n", dc.UserCode)
	fmt.Printf("  Waiting for authorization...\n")

	interval := time.Duration(dc.Interval) * time.Second
	if interval < 5*time.Second {
		interval = 5 * time.Second
	}

	deadline := time.Now().Add(time.Duration(dc.ExpiresIn) * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(interval):
		}

		ts, err := pollToken(ctx, tokenURL, dc.DeviceCode)
		if err != nil {
			if isAuthorizationPending(err) {
				continue
			}
			return nil, fmt.Errorf("poll token: %w", err)
		}
		ts.ClusterURL = clusterURL

		if err := StoreToken(ts); err != nil {
			return nil, fmt.Errorf("store token: %w", err)
		}
		return ts, nil
	}
	return nil, fmt.Errorf("device code expired; please run `gough login` again")
}

// LoadToken returns the current token, preferring the GOUGH_TOKEN env var,
// then the OS keychain.  Returns an error if no token is found.
func LoadToken() (*TokenSet, error) {
	// Env var override — never log or print the raw value.
	if t := os.Getenv(envVarToken); t != "" {
		return &TokenSet{AccessToken: t}, nil
	}

	raw, err := keyring.Get(keychainService, keychainUser)
	if err != nil {
		return nil, fmt.Errorf("no stored credentials; run `gough login` first: %w", err)
	}

	var p tokenPayload
	if err := json.Unmarshal([]byte(raw), &p); err != nil {
		return nil, fmt.Errorf("malformed stored token; run `gough login` again: %w", err)
	}

	return &TokenSet{
		AccessToken:  p.AccessToken,
		RefreshToken: p.RefreshToken,
		ExpiresAt:    p.ExpiresAt,
		ClusterURL:   p.ClusterURL,
	}, nil
}

// StoreToken persists the token to the OS keychain.
// The raw access token is never written to stdout, logs, or disk plaintext.
func StoreToken(ts *TokenSet) error {
	p := tokenPayload{
		AccessToken:  ts.AccessToken,
		RefreshToken: ts.RefreshToken,
		ExpiresAt:    ts.ExpiresAt,
		ClusterURL:   ts.ClusterURL,
	}
	raw, err := json.Marshal(p)
	if err != nil {
		return err
	}
	return keyring.Set(keychainService, keychainUser, string(raw))
}

// DeleteToken removes the stored token from the keychain.
func DeleteToken() error {
	return keyring.Delete(keychainService, keychainUser)
}

// NeedsRefresh reports whether the access token will expire within 5 minutes.
// Callers should check NeedsRefresh() before each API call and call RefreshToken()
// if true and RefreshToken is non-empty.
func (ts *TokenSet) NeedsRefresh() bool {
	if ts.ExpiresAt.IsZero() {
		return false
	}
	return time.Until(ts.ExpiresAt) < 5*time.Minute
}

// RefreshToken exchanges a refresh token for a new TokenSet and persists it.
// Returns an error if no refresh token is available or if the refresh fails.
func RefreshToken(ctx context.Context, ts *TokenSet) (*TokenSet, error) {
	if ts.RefreshToken == "" {
		return nil, fmt.Errorf("no refresh token available; run `gough login`")
	}

	if ts.ClusterURL == "" {
		return nil, fmt.Errorf("no cluster URL in stored token; run `gough login`")
	}

	tokenURL := strings.TrimRight(ts.ClusterURL, "/") + "/api/v1/auth/token"

	resp, err := httpPost(ctx, tokenURL, url.Values{
		"client_id":     {"gough-cli"},
		"grant_type":    {"refresh_token"},
		"refresh_token": {ts.RefreshToken},
	})
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("token endpoint HTTP %d: %s", resp.StatusCode, body)
	}

	var raw struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		ExpiresIn    int    `json:"expires_in"`
		TokenType    string `json:"token_type"`
		Scope        string `json:"scope"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, fmt.Errorf("decode token response: %w", err)
	}

	expiresAt := time.Time{}
	if raw.ExpiresIn > 0 {
		expiresAt = time.Now().Add(time.Duration(raw.ExpiresIn) * time.Second)
	}

	newTS := &TokenSet{
		AccessToken:  raw.AccessToken,
		RefreshToken: raw.RefreshToken,
		TokenType:    raw.TokenType,
		ExpiresAt:    expiresAt,
		Scope:        raw.Scope,
		ClusterURL:   ts.ClusterURL,
	}

	if err := StoreToken(newTS); err != nil {
		return nil, fmt.Errorf("store refreshed token: %w", err)
	}

	return newTS, nil
}

// MaskToken returns the access token masked as tok_****<last4>.
// Never use the raw token in log output; always call MaskToken first.
func (ts *TokenSet) MaskToken() string {
	t := ts.AccessToken
	if len(t) < 4 {
		return "tok_****"
	}
	return "tok_****" + t[len(t)-4:]
}

// startDeviceFlow calls the cluster's device-authorization endpoint and
// returns the DeviceCodeResponse plus the token endpoint URL.
func startDeviceFlow(ctx context.Context, clusterURL string) (*DeviceCodeResponse, string, error) {
	// The Gough cluster exposes an OIDC-compatible device auth endpoint.
	deviceAuthURL := strings.TrimRight(clusterURL, "/") + "/api/v1/auth/device"
	tokenURL := strings.TrimRight(clusterURL, "/") + "/api/v1/auth/token"

	resp, err := httpPost(ctx, deviceAuthURL, url.Values{
		"client_id": {"gough-cli"},
		"scope":     {"openid offline_access gough.operator"},
	})
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, "", fmt.Errorf("device auth HTTP %d: %s", resp.StatusCode, body)
	}

	var dc DeviceCodeResponse
	if err := json.NewDecoder(resp.Body).Decode(&dc); err != nil {
		return nil, "", fmt.Errorf("decode device auth response: %w", err)
	}
	return &dc, tokenURL, nil
}

// pollToken polls the token endpoint with the device code.
func pollToken(ctx context.Context, tokenURL, deviceCode string) (*TokenSet, error) {
	resp, err := httpPost(ctx, tokenURL, url.Values{
		"client_id":   {"gough-cli"},
		"device_code": {deviceCode},
		"grant_type":  {"urn:ietf:params:oauth:grant-type:device_code"},
	})
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == http.StatusBadRequest {
		// Check for authorization_pending or slow_down.
		var errResp struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(body, &errResp)
		if errResp.Error == "authorization_pending" || errResp.Error == "slow_down" {
			return nil, &pendingError{errResp.Error}
		}
		return nil, fmt.Errorf("token endpoint: %s", body)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("token endpoint HTTP %d: %s", resp.StatusCode, body)
	}

	var raw struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		ExpiresIn    int    `json:"expires_in"`
		TokenType    string `json:"token_type"`
		Scope        string `json:"scope"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, fmt.Errorf("decode token response: %w", err)
	}

	expiresAt := time.Time{}
	if raw.ExpiresIn > 0 {
		expiresAt = time.Now().Add(time.Duration(raw.ExpiresIn) * time.Second)
	}

	return &TokenSet{
		AccessToken:  raw.AccessToken,
		RefreshToken: raw.RefreshToken,
		TokenType:    raw.TokenType,
		ExpiresAt:    expiresAt,
		Scope:        raw.Scope,
	}, nil
}

func httpPost(ctx context.Context, endpoint string, vals url.Values) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint,
		strings.NewReader(vals.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	client := &http.Client{Timeout: 30 * time.Second}
	return client.Do(req)
}

// pendingError is returned when the authorization endpoint indicates the
// user has not yet completed authorization.
type pendingError struct {
	code string
}

func (e *pendingError) Error() string { return e.code }

func isAuthorizationPending(err error) bool {
	if p, ok := err.(*pendingError); ok {
		return p.code == "authorization_pending" || p.code == "slow_down"
	}
	return false
}
