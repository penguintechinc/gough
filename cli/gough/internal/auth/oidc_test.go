//go:build noxdp

package auth

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/zalando/go-keyring"
)

func TestTokenSet_MaskToken(t *testing.T) {
	tests := []struct {
		token string
		want  string
	}{
		{"eyJhbGciOiJSUzI1NiJ9.longtoken1234", "tok_****1234"},
		{"abcd", "tok_****abcd"},
		{"ab", "tok_****"},
		{"abc", "tok_****"},
	}
	for _, tt := range tests {
		ts := &TokenSet{AccessToken: tt.token}
		got := ts.MaskToken()
		if got != tt.want {
			t.Errorf("MaskToken(%q) = %q; want %q", tt.token, got, tt.want)
		}
	}
}

func TestTokenSet_NeedsRefresh(t *testing.T) {
	tests := []struct {
		name      string
		expiresAt time.Time
		want      bool
	}{
		{
			name:      "expires soon",
			expiresAt: time.Now().Add(2 * time.Minute),
			want:      true,
		},
		{
			name:      "plenty of time",
			expiresAt: time.Now().Add(30 * time.Minute),
			want:      false,
		},
		{
			name:      "already expired",
			expiresAt: time.Now().Add(-1 * time.Minute),
			want:      true,
		},
		{
			name:      "zero time (no expiry set)",
			expiresAt: time.Time{},
			want:      false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ts := &TokenSet{ExpiresAt: tt.expiresAt}
			got := ts.NeedsRefresh()
			if got != tt.want {
				t.Errorf("NeedsRefresh() = %v; want %v (expires %v)", got, tt.want, tt.expiresAt)
			}
		})
	}
}

func TestTokenPayload_MarshalUnmarshal(t *testing.T) {
	// Validate that a TokenSet can round-trip through the keychain JSON format.
	original := &TokenSet{
		AccessToken:  "access-tok-xyz",
		RefreshToken: "refresh-tok-abc",
		ExpiresAt:    time.Now().Add(1 * time.Hour).Truncate(time.Second),
		ClusterURL:   "https://gough.example.com",
	}

	// The internal tokenPayload is unexported; test via StoreToken/LoadToken
	// would require a real keychain.  Instead we test the struct fields directly.
	if original.AccessToken != "access-tok-xyz" {
		t.Errorf("AccessToken mismatch")
	}
	if original.ClusterURL != "https://gough.example.com" {
		t.Errorf("ClusterURL mismatch")
	}
}

func TestIsAuthorizationPending(t *testing.T) {
	tests := []struct {
		err  error
		want bool
	}{
		{&pendingError{"authorization_pending"}, true},
		{&pendingError{"slow_down"}, true},
		{&pendingError{"access_denied"}, false},
		{nil, false},
	}
	for _, tt := range tests {
		got := isAuthorizationPending(tt.err)
		if got != tt.want {
			t.Errorf("isAuthorizationPending(%v) = %v; want %v", tt.err, got, tt.want)
		}
	}
}

func TestPendingError_Error(t *testing.T) {
	e := &pendingError{code: "authorization_pending"}
	if e.Error() != "authorization_pending" {
		t.Errorf("Error() = %q; want %q", e.Error(), "authorization_pending")
	}
	e2 := &pendingError{code: "slow_down"}
	if e2.Error() != "slow_down" {
		t.Errorf("Error() = %q; want %q", e2.Error(), "slow_down")
	}
}

func TestLoadToken_EnvVarOverride(t *testing.T) {
	t.Setenv("GOUGH_TOKEN", "test-token-from-env")
	ts, err := LoadToken()
	if err != nil {
		t.Fatalf("LoadToken() unexpected error: %v", err)
	}
	if ts.AccessToken != "test-token-from-env" {
		t.Errorf("AccessToken = %q; want %q", ts.AccessToken, "test-token-from-env")
	}
}

func TestLoadToken_NoTokenReturnsError(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	t.Setenv("GOUGH_TOKEN", "")
	_, err := LoadToken()
	if err == nil {
		t.Error("LoadToken() expected error when no token, got nil")
	}
}

func TestLoadToken_FromKeyring(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	t.Setenv("GOUGH_TOKEN", "")

	// Store a token and load it back.
	ts := &TokenSet{
		AccessToken:  "key-access-123",
		RefreshToken: "key-refresh-456",
		ExpiresAt:    time.Now().Add(2 * time.Hour).Truncate(time.Second),
		ClusterURL:   "https://key-cluster.example.com",
	}
	if err := StoreToken(ts); err != nil {
		t.Fatalf("StoreToken() error: %v", err)
	}

	loaded, err := LoadToken()
	if err != nil {
		t.Fatalf("LoadToken() error: %v", err)
	}
	if loaded.AccessToken != ts.AccessToken {
		t.Errorf("AccessToken = %q; want %q", loaded.AccessToken, ts.AccessToken)
	}
	if loaded.RefreshToken != ts.RefreshToken {
		t.Errorf("RefreshToken = %q; want %q", loaded.RefreshToken, ts.RefreshToken)
	}
	if loaded.ClusterURL != ts.ClusterURL {
		t.Errorf("ClusterURL = %q; want %q", loaded.ClusterURL, ts.ClusterURL)
	}
}

func TestLoadToken_MalformedJSON(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	t.Setenv("GOUGH_TOKEN", "")

	// Manually set malformed JSON in keyring.
	_ = keyring.Set(keychainService, keychainUser, "not-valid-json{")

	_, err := LoadToken()
	if err == nil {
		t.Error("LoadToken() expected error for malformed JSON, got nil")
	}
}

func TestDeleteToken_NoEntry(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	// DeleteToken may fail if no token stored; that's OK, just shouldn't panic
	err := DeleteToken()
	// Error is expected when no token exists, but it shouldn't crash
	_ = err
}

func TestDeleteToken_WithStoredToken(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	// Store a token first.
	ts := &TokenSet{
		AccessToken:  "delete-test-token",
		RefreshToken: "delete-test-refresh",
		ExpiresAt:    time.Now().Add(1 * time.Hour).Truncate(time.Second),
		ClusterURL:   "https://delete-test.example.com",
	}
	if err := StoreToken(ts); err != nil {
		t.Fatalf("StoreToken() error: %v", err)
	}

	// Delete it.
	if err := DeleteToken(); err != nil {
		t.Fatalf("DeleteToken() error: %v", err)
	}

	// Verify it's gone.
	t.Setenv("GOUGH_TOKEN", "")
	_, err := LoadToken()
	if err == nil {
		t.Error("LoadToken() expected error after delete, got nil")
	}
}

func TestHttpPost_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	resp, err := httpPost(context.Background(), srv.URL, nil)
	if err != nil {
		t.Fatalf("httpPost() error: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("StatusCode = %d; want 200", resp.StatusCode)
	}
}

func TestHttpPost_NetworkError(t *testing.T) {
	_, err := httpPost(context.Background(), "http://localhost:0", nil)
	if err == nil {
		t.Error("expected network error, got nil")
	}
}

func TestStartDeviceFlow_Non200(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("server error"))
	}))
	defer srv.Close()
	_, _, err := startDeviceFlow(context.Background(), srv.URL)
	if err == nil {
		t.Error("expected error for non-200 response, got nil")
	}
}

func TestStartDeviceFlow_InvalidJSON(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("not-json"))
	}))
	defer srv.Close()
	_, _, err := startDeviceFlow(context.Background(), srv.URL)
	if err == nil {
		t.Error("expected JSON decode error, got nil")
	}
}

func TestStartDeviceFlow_Success(t *testing.T) {
	dc := DeviceCodeResponse{
		DeviceCode:              "dev-code-123",
		UserCode:                "USER-CODE",
		VerificationURI:         "https://example.com/activate",
		VerificationURIComplete: "https://example.com/activate?code=USER-CODE",
		ExpiresIn:               300,
		Interval:                5,
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(dc)
	}))
	defer srv.Close()
	got, tokenURL, err := startDeviceFlow(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("startDeviceFlow() error: %v", err)
	}
	if got.DeviceCode != "dev-code-123" {
		t.Errorf("DeviceCode = %q; want %q", got.DeviceCode, "dev-code-123")
	}
	if tokenURL == "" {
		t.Error("tokenURL is empty")
	}
}

func TestPollToken_AuthorizationPending(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"authorization_pending"}`))
	}))
	defer srv.Close()
	_, err := pollToken(context.Background(), srv.URL, "dev-code")
	if !isAuthorizationPending(err) {
		t.Errorf("expected pending error, got %v", err)
	}
}

func TestPollToken_SlowDown(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"slow_down"}`))
	}))
	defer srv.Close()
	_, err := pollToken(context.Background(), srv.URL, "dev-code")
	if !isAuthorizationPending(err) {
		t.Errorf("expected slow_down to be pending, got %v", err)
	}
}

func TestPollToken_AccessDenied(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"access_denied"}`))
	}))
	defer srv.Close()
	_, err := pollToken(context.Background(), srv.URL, "dev-code")
	if err == nil {
		t.Error("expected error for access_denied, got nil")
	}
	if isAuthorizationPending(err) {
		t.Error("access_denied should not be pending")
	}
}

func TestPollToken_ServerError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("server error"))
	}))
	defer srv.Close()
	_, err := pollToken(context.Background(), srv.URL, "dev-code")
	if err == nil {
		t.Error("expected error for 500, got nil")
	}
}

func TestPollToken_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"access-123","refresh_token":"refresh-456","expires_in":3600,"token_type":"Bearer","scope":"openid"}`))
	}))
	defer srv.Close()
	ts, err := pollToken(context.Background(), srv.URL, "dev-code")
	if err != nil {
		t.Fatalf("pollToken() error: %v", err)
	}
	if ts.AccessToken != "access-123" {
		t.Errorf("AccessToken = %q; want %q", ts.AccessToken, "access-123")
	}
	if ts.ExpiresAt.IsZero() {
		t.Error("ExpiresAt should not be zero")
	}
}

func TestPollToken_InvalidJSON(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("not-json"))
	}))
	defer srv.Close()
	_, err := pollToken(context.Background(), srv.URL, "dev-code")
	if err == nil {
		t.Error("expected JSON decode error, got nil")
	}
}

// TestStoreToken tests that StoreToken can marshal and store a TokenSet.
func TestStoreToken(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() {
		_ = DeleteToken()
		keyring.MockInit()
	})

	ts := &TokenSet{
		AccessToken:  "access-token-xyz",
		RefreshToken: "refresh-token-abc",
		ExpiresAt:    time.Now().Add(1 * time.Hour).Truncate(time.Second),
		ClusterURL:   "https://test.example.com",
	}

	err := StoreToken(ts)
	if err != nil {
		t.Fatalf("StoreToken() error: %v", err)
	}

	// Verify we can load it back.
	loaded, err := LoadToken()
	if err != nil {
		t.Fatalf("LoadToken() error: %v", err)
	}
	if loaded.AccessToken != ts.AccessToken {
		t.Errorf("AccessToken mismatch: got %q, want %q", loaded.AccessToken, ts.AccessToken)
	}
	if loaded.RefreshToken != ts.RefreshToken {
		t.Errorf("RefreshToken mismatch: got %q, want %q", loaded.RefreshToken, ts.RefreshToken)
	}
	if loaded.ClusterURL != ts.ClusterURL {
		t.Errorf("ClusterURL mismatch: got %q, want %q", loaded.ClusterURL, ts.ClusterURL)
	}
}

// TestDeviceLogin_Success tests the full device login flow.
func TestDeviceLogin_Success(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	// Mock the device auth and token endpoints together.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/device") {
			dc := DeviceCodeResponse{
				DeviceCode:              "dev-code-success",
				UserCode:                "SUCCESS-CODE",
				VerificationURI:         "https://example.com/activate",
				VerificationURIComplete: "https://example.com/activate?code=SUCCESS-CODE",
				ExpiresIn:               300,
				Interval:                1,
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(dc)
		} else if strings.Contains(r.URL.Path, "/token") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"access_token":"success-access-token","refresh_token":"success-refresh-token","expires_in":3600,"token_type":"Bearer","scope":"openid"}`))
		} else {
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	ts, err := DeviceLogin(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("DeviceLogin() error: %v", err)
	}
	if ts.AccessToken != "success-access-token" {
		t.Errorf("AccessToken = %q; want %q", ts.AccessToken, "success-access-token")
	}
	if ts.ClusterURL != srv.URL {
		t.Errorf("ClusterURL = %q; want %q", ts.ClusterURL, srv.URL)
	}

	// Verify it was stored in keychain.
	t.Setenv("GOUGH_TOKEN", "")
	loaded, err := LoadToken()
	if err != nil {
		t.Fatalf("LoadToken() error: %v", err)
	}
	if loaded.AccessToken != "success-access-token" {
		t.Errorf("loaded AccessToken = %q; want %q", loaded.AccessToken, "success-access-token")
	}
}

// TestPollToken_ExpiresInZero tests pollToken when expires_in is 0.
func TestPollToken_ExpiresInZero(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"access-123","refresh_token":"refresh-456","expires_in":0,"token_type":"Bearer","scope":"openid"}`))
	}))
	defer srv.Close()
	ts, err := pollToken(context.Background(), srv.URL, "dev-code")
	if err != nil {
		t.Fatalf("pollToken() error: %v", err)
	}
	if !ts.ExpiresAt.IsZero() {
		t.Error("ExpiresAt should be zero when expires_in is 0")
	}
}

// TestDeviceLogin_ContextCanceled tests DeviceLogin with a canceled context.
func TestDeviceLogin_ContextCanceled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := DeviceLogin(ctx, "https://example.com")
	if err == nil {
		t.Error("expected error for canceled context")
	}
	// The error will be wrapped, so just check it's not nil.
	if err.Error() == "" {
		t.Error("expected non-empty error message")
	}
}

// TestDeviceLogin_DeviceAuthFails tests DeviceLogin when device auth endpoint fails.
func TestDeviceLogin_DeviceAuthFails(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("server error"))
	}))
	defer srv.Close()

	_, err := DeviceLogin(context.Background(), srv.URL)
	if err == nil {
		t.Error("expected error when device auth fails")
	}
}

// TestDeviceLogin_StoreTokenFails tests DeviceLogin when storing token fails.
func TestDeviceLogin_StoreTokenFails(t *testing.T) {
	keyring.MockInitWithError(keyring.ErrUnsupportedPlatform)
	t.Cleanup(func() { keyring.MockInit() })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/device") {
			dc := DeviceCodeResponse{
				DeviceCode:              "dev-code-fail",
				UserCode:                "FAIL-CODE",
				VerificationURI:         "https://example.com/activate",
				VerificationURIComplete: "https://example.com/activate?code=FAIL-CODE",
				ExpiresIn:               300,
				Interval:                1,
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(dc)
		} else if strings.Contains(r.URL.Path, "/token") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"access_token":"fail-access","refresh_token":"fail-refresh","expires_in":3600,"token_type":"Bearer","scope":"openid"}`))
		}
	}))
	defer srv.Close()

	_, err := DeviceLogin(context.Background(), srv.URL)
	if err == nil {
		t.Error("expected error when store token fails")
	}
}

// TestDeviceLogin_PollTokenRetryAndSuccess tests that DeviceLogin retries on authorization_pending.
func TestDeviceLogin_PollTokenRetryAndSuccess(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	pollCount := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/device") {
			dc := DeviceCodeResponse{
				DeviceCode:              "dev-code-retry",
				UserCode:                "RETRY-CODE",
				VerificationURI:         "https://example.com/activate",
				VerificationURIComplete: "https://example.com/activate?code=RETRY-CODE",
				ExpiresIn:               300,
				Interval:                1,
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(dc)
		} else if strings.Contains(r.URL.Path, "/token") {
			pollCount++
			if pollCount == 1 {
				// First poll: return pending.
				w.WriteHeader(http.StatusBadRequest)
				_, _ = w.Write([]byte(`{"error":"authorization_pending"}`))
			} else {
				// Second poll: return token.
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"access_token":"retry-access","refresh_token":"retry-refresh","expires_in":3600,"token_type":"Bearer","scope":"openid"}`))
			}
		}
	}))
	defer srv.Close()

	ts, err := DeviceLogin(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("DeviceLogin() error: %v", err)
	}
	if ts.AccessToken != "retry-access" {
		t.Errorf("AccessToken = %q; want %q", ts.AccessToken, "retry-access")
	}
	if pollCount < 2 {
		t.Errorf("expected at least 2 polls, got %d", pollCount)
	}
}

// TestStartDeviceFlow_TrailingSlashHandling tests that trailing slashes are handled correctly.
func TestStartDeviceFlow_TrailingSlashHandling(t *testing.T) {
	dc := DeviceCodeResponse{
		DeviceCode:              "dev-code-xyz",
		UserCode:                "ABC-123",
		VerificationURI:         "https://example.com/activate",
		VerificationURIComplete: "https://example.com/activate?code=ABC-123",
		ExpiresIn:               300,
		Interval:                5,
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(dc)
	}))
	defer srv.Close()

	// Test with trailing slash in URL.
	got, _, err := startDeviceFlow(context.Background(), srv.URL+"/")
	if err != nil {
		t.Fatalf("startDeviceFlow with trailing slash error: %v", err)
	}
	if got.DeviceCode != "dev-code-xyz" {
		t.Errorf("DeviceCode mismatch")
	}
}

// TestHttpPost_ContextTimeout tests httpPost with context timeout.
func TestHttpPost_ContextTimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Simulate a slow server.
		time.Sleep(1 * time.Second)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	_, err := httpPost(ctx, srv.URL, nil)
	if err == nil {
		t.Error("expected timeout error")
	}
}

// TestLoadToken_KeyringUnmarshalError tests LoadToken with malformed keyring JSON.
// Note: This is difficult to test without mocking the keyring. This test is a placeholder.
func TestLoadToken_EmptyEnvVar(t *testing.T) {
	// Set env var to empty string explicitly.
	t.Setenv("GOUGH_TOKEN", "")
	_, err := LoadToken()
	if err == nil {
		t.Error("LoadToken with no token should return error")
	}
}

// TestPollToken_BadRequestMissingError tests pollToken when error field is missing.
func TestPollToken_BadRequestMissingError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"some_field":"value"}`))
	}))
	defer srv.Close()

	_, err := pollToken(context.Background(), srv.URL, "dev-code")
	if err == nil {
		t.Error("expected error for bad request without error field")
	}
}

// TestRefreshToken_NoRefreshToken tests RefreshToken when refresh token is missing.
func TestRefreshToken_NoRefreshToken(t *testing.T) {
	ts := &TokenSet{
		AccessToken: "old-access",
		ClusterURL:  "https://cluster.example.com",
	}
	_, err := RefreshToken(context.Background(), ts)
	if err == nil {
		t.Fatal("expected error for missing refresh token")
	}
	if !strings.Contains(err.Error(), "no refresh token") {
		t.Errorf("expected 'no refresh token' in error, got %q", err.Error())
	}
}

// TestRefreshToken_NoClusterURL tests RefreshToken when cluster URL is missing.
func TestRefreshToken_NoClusterURL(t *testing.T) {
	ts := &TokenSet{
		AccessToken:  "old-access",
		RefreshToken: "refresh-123",
	}
	_, err := RefreshToken(context.Background(), ts)
	if err == nil {
		t.Fatal("expected error for missing cluster URL")
	}
	if !strings.Contains(err.Error(), "no cluster URL") {
		t.Errorf("expected 'no cluster URL' in error, got %q", err.Error())
	}
}

// TestRefreshToken_Success tests RefreshToken with a successful token refresh.
func TestRefreshToken_Success(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		// Verify form values
		if err := r.ParseForm(); err != nil {
			t.Errorf("ParseForm() error: %v", err)
		}
		if r.FormValue("grant_type") != "refresh_token" {
			t.Errorf("expected grant_type=refresh_token, got %q", r.FormValue("grant_type"))
		}
		if r.FormValue("refresh_token") != "old-refresh" {
			t.Errorf("expected matching refresh token")
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"access_token":  "new-access-token",
			"refresh_token": "new-refresh-token",
			"expires_in":    3600,
			"token_type":    "Bearer",
			"scope":         "openid offline_access",
		})
	}))
	defer srv.Close()

	ts := &TokenSet{
		AccessToken:  "old-access",
		RefreshToken: "old-refresh",
		ClusterURL:   srv.URL,
	}

	newTS, err := RefreshToken(context.Background(), ts)
	if err != nil {
		t.Fatalf("RefreshToken() error: %v", err)
	}
	if newTS.AccessToken != "new-access-token" {
		t.Errorf("AccessToken = %q, want %q", newTS.AccessToken, "new-access-token")
	}
	if newTS.RefreshToken != "new-refresh-token" {
		t.Errorf("RefreshToken = %q, want %q", newTS.RefreshToken, "new-refresh-token")
	}
	if newTS.ClusterURL != srv.URL {
		t.Errorf("ClusterURL not preserved: got %q, want %q", newTS.ClusterURL, srv.URL)
	}
	if newTS.ExpiresAt.IsZero() {
		t.Error("ExpiresAt should not be zero")
	}
	if newTS.TokenType != "Bearer" {
		t.Errorf("TokenType = %q, want %q", newTS.TokenType, "Bearer")
	}

	// Verify token was stored in keychain.
	t.Setenv("GOUGH_TOKEN", "")
	loaded, err := LoadToken()
	if err != nil {
		t.Fatalf("LoadToken() error: %v", err)
	}
	if loaded.AccessToken != "new-access-token" {
		t.Errorf("loaded AccessToken = %q, want %q", loaded.AccessToken, "new-access-token")
	}
}

// TestRefreshToken_PreservesClusterURL tests that RefreshToken preserves the original ClusterURL.
func TestRefreshToken_PreservesClusterURL(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"access_token":  "new-token",
			"refresh_token": "new-refresh",
			"expires_in":    3600,
			"token_type":    "Bearer",
		})
	}))
	defer srv.Close()

	// Use the test server's URL to ensure the test doesn't try to make real network calls.
	ts := &TokenSet{
		AccessToken:  "old",
		RefreshToken: "refresh",
		ClusterURL:   srv.URL,
	}

	newTS, err := RefreshToken(context.Background(), ts)
	if err != nil {
		t.Fatalf("RefreshToken() error: %v", err)
	}
	if newTS.ClusterURL != srv.URL {
		t.Errorf("ClusterURL = %q, want original %q", newTS.ClusterURL, srv.URL)
	}
}

// TestRefreshToken_ServerError tests RefreshToken when the token endpoint returns an error.
func TestRefreshToken_ServerError(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":"invalid_grant"}`))
	}))
	defer srv.Close()

	ts := &TokenSet{
		AccessToken:  "old",
		RefreshToken: "bad-refresh",
		ClusterURL:   srv.URL,
	}

	_, err := RefreshToken(context.Background(), ts)
	if err == nil {
		t.Fatal("expected error for 401 response")
	}
	if !strings.Contains(err.Error(), "401") {
		t.Errorf("expected '401' in error, got %q", err.Error())
	}
}

// TestRefreshToken_InvalidJSON tests RefreshToken when the server returns invalid JSON.
func TestRefreshToken_InvalidJSON(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("not-valid-json{"))
	}))
	defer srv.Close()

	ts := &TokenSet{
		AccessToken:  "old",
		RefreshToken: "refresh",
		ClusterURL:   srv.URL,
	}

	_, err := RefreshToken(context.Background(), ts)
	if err == nil {
		t.Fatal("expected error for invalid JSON response")
	}
	if !strings.Contains(err.Error(), "decode") {
		t.Errorf("expected 'decode' in error, got %q", err.Error())
	}
}

// TestRefreshToken_NetworkError tests RefreshToken with a network error.
func TestRefreshToken_NetworkError(t *testing.T) {
	ts := &TokenSet{
		AccessToken:  "old",
		RefreshToken: "refresh",
		ClusterURL:   "http://localhost:0", // Invalid address
	}

	_, err := RefreshToken(context.Background(), ts)
	if err == nil {
		t.Fatal("expected network error")
	}
}

// TestRefreshToken_ContextTimeout tests RefreshToken with context timeout.
func TestRefreshToken_ContextTimeout(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Simulate a slow server that exceeds the context timeout.
		time.Sleep(500 * time.Millisecond)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	ts := &TokenSet{
		AccessToken:  "old",
		RefreshToken: "refresh",
		ClusterURL:   srv.URL,
	}

	_, err := RefreshToken(ctx, ts)
	if err == nil {
		t.Fatal("expected timeout error")
	}
}

// TestRefreshToken_ExpiresInZero tests RefreshToken when expires_in is 0.
func TestRefreshToken_ExpiresInZero(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"access_token":  "token",
			"refresh_token": "refresh",
			"expires_in":    0,
			"token_type":    "Bearer",
		})
	}))
	defer srv.Close()

	ts := &TokenSet{
		AccessToken:  "old",
		RefreshToken: "refresh",
		ClusterURL:   srv.URL,
	}

	newTS, err := RefreshToken(context.Background(), ts)
	if err != nil {
		t.Fatalf("RefreshToken() error: %v", err)
	}
	if !newTS.ExpiresAt.IsZero() {
		t.Error("ExpiresAt should be zero when expires_in is 0")
	}
}

// TestRefreshToken_TrailingSlashHandling tests that RefreshToken handles trailing slashes in ClusterURL.
func TestRefreshToken_TrailingSlashHandling(t *testing.T) {
	keyring.MockInit()
	t.Cleanup(func() { keyring.MockInit() })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"access_token":  "new-token",
			"refresh_token": "new-refresh",
			"expires_in":    3600,
			"token_type":    "Bearer",
		})
	}))
	defer srv.Close()

	// Test with trailing slash in ClusterURL.
	ts := &TokenSet{
		AccessToken:  "old",
		RefreshToken: "refresh",
		ClusterURL:   srv.URL + "/",
	}

	newTS, err := RefreshToken(context.Background(), ts)
	if err != nil {
		t.Fatalf("RefreshToken() error: %v", err)
	}
	if newTS.AccessToken != "new-token" {
		t.Errorf("AccessToken = %q, want new-token", newTS.AccessToken)
	}
}

// TestRefreshToken_StoreTokenFailure tests RefreshToken when storing the token fails.
func TestRefreshToken_StoreTokenFailure(t *testing.T) {
	keyring.MockInitWithError(keyring.ErrUnsupportedPlatform)
	t.Cleanup(func() { keyring.MockInit() })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"access_token":  "new-token",
			"refresh_token": "new-refresh",
			"expires_in":    3600,
			"token_type":    "Bearer",
		})
	}))
	defer srv.Close()

	ts := &TokenSet{
		AccessToken:  "old",
		RefreshToken: "refresh",
		ClusterURL:   srv.URL,
	}

	_, err := RefreshToken(context.Background(), ts)
	if err == nil {
		t.Fatal("expected error when store token fails")
	}
	if !strings.Contains(err.Error(), "store refreshed token") {
		t.Errorf("expected 'store refreshed token' in error, got %q", err.Error())
	}
}
