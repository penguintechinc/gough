//go:build noxdp

// Package client provides a hand-rolled HTTP client for the Gough API.
// This will be replaced by an oapi-codegen generated client once the
// canonical openapi.json is stabilised (see Makefile target `make sdk`).
//
// Token hygiene: tokens are injected via the Authorization header and NEVER
// logged.  All log references use the masked form (tok_****<last4>).
package client

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math/rand"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// ClientOption is a functional option for configuring Client.
type ClientOption func(*Client)

// WithVerbose enables debug logging to stderr.
func WithVerbose(v bool) ClientOption {
	return func(c *Client) {
		c.verbose = v
		if v {
			c.logger = slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug}))
		}
	}
}

// WithTLSCAFile sets a custom CA certificate file for TLS verification.
func WithTLSCAFile(path string) ClientOption {
	return func(c *Client) {
		c.tlsCAFile = path
	}
}

// WithInsecureSkipVerify disables TLS certificate verification (dangerous; use only for testing).
func WithInsecureSkipVerify(skip bool) ClientOption {
	return func(c *Client) {
		c.insecureSkipTLS = skip
	}
}

// Client is a Gough REST API client.
type Client struct {
	baseURL         string
	token           string // raw access token — never logged
	tenantID        string // X-Tenant-ID header override
	httpClient      *http.Client
	verbose         bool
	tlsCAFile       string
	insecureSkipTLS bool
	logger          *slog.Logger // nil when not verbose
}

// APIResponse wraps every Gough API response envelope.
type APIResponse struct {
	Status string          `json:"status"`
	Data   json.RawMessage `json:"data"`
	Meta   *APIMeta        `json:"meta,omitempty"`
	Error  string          `json:"error,omitempty"`
	Note   string          `json:"note,omitempty"` // deferred-note on 202
}

// APIMeta contains response metadata.
type APIMeta struct {
	Version   int    `json:"version"`
	Timestamp string `json:"timestamp"`
	Total     int    `json:"total,omitempty"`
	Page      int    `json:"page,omitempty"`
	PageSize  int    `json:"page_size,omitempty"`
}

// New creates a Client.  clusterURL should be the base URL including path prefix,
// e.g. "https://gough.example.com".  token is the bearer token (access token only).
// Options can be passed to configure TLS, verbosity, and other settings.
func New(clusterURL, token string, opts ...ClientOption) *Client {
	c := &Client{
		baseURL: strings.TrimRight(clusterURL, "/") + "/api/v1",
		token:   token,
		httpClient: &http.Client{
			Timeout: 60 * time.Second,
		},
	}

	// Apply options.
	for _, opt := range opts {
		opt(c)
	}

	// Build custom transport if TLS options are set.
	if c.tlsCAFile != "" || c.insecureSkipTLS {
		tlsCfg := &tls.Config{}

		if c.tlsCAFile != "" {
			certPEM, err := os.ReadFile(c.tlsCAFile)
			if err == nil {
				certPool := x509.NewCertPool()
				if certPool.AppendCertsFromPEM(certPEM) {
					tlsCfg.RootCAs = certPool
				}
			}
		}

		if c.insecureSkipTLS {
			tlsCfg.InsecureSkipVerify = true
		}

		c.httpClient.Transport = &http.Transport{
			TLSClientConfig: tlsCfg,
		}
	}

	return c
}

// get performs a GET request and returns the parsed APIResponse.
func (c *Client) get(ctx context.Context, path string, params url.Values) (*APIResponse, error) {
	endpoint := c.baseURL + path
	if len(params) > 0 {
		endpoint += "?" + params.Encode()
	}
	return c.doWithRetry(ctx, http.MethodGet, endpoint, nil)
}

// post performs a POST request with a JSON body.
func (c *Client) post(ctx context.Context, path string, body interface{}) (*APIResponse, error) {
	return c.doJSON(ctx, http.MethodPost, path, body)
}

// put performs a PUT request with a JSON body.
func (c *Client) put(ctx context.Context, path string, body interface{}) (*APIResponse, error) {
	return c.doJSON(ctx, http.MethodPut, path, body)
}

// patch performs a PATCH request with a JSON body.
func (c *Client) patch(ctx context.Context, path string, body interface{}) (*APIResponse, error) {
	return c.doJSON(ctx, http.MethodPatch, path, body)
}

// del performs a DELETE request.
func (c *Client) del(ctx context.Context, path string) (*APIResponse, error) {
	endpoint := c.baseURL + path
	return c.doWithRetry(ctx, http.MethodDelete, endpoint, nil)
}

func (c *Client) doJSON(ctx context.Context, method, path string, body interface{}) (*APIResponse, error) {
	var bodyBytes []byte
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("marshal request: %w", err)
		}
		bodyBytes = b
	}
	endpoint := c.baseURL + path
	return c.doWithRetry(ctx, method, endpoint, bodyBytes)
}

// doWithRetry executes a request with retry logic for 429 (rate limit) responses.
// bodyBytes is the pre-marshalled request body (may be nil for GET/DELETE).
func (c *Client) doWithRetry(ctx context.Context, method, endpoint string, bodyBytes []byte) (*APIResponse, error) {
	const maxRetries = 3
	const baseDelay = 1 * time.Second
	const maxDelay = 30 * time.Second

	for attempt := 0; attempt <= maxRetries; attempt++ {
		// Create a fresh request for this attempt (body reader consumed after first use).
		var r io.Reader
		if bodyBytes != nil {
			r = bytes.NewReader(bodyBytes)
		}

		req, err := http.NewRequestWithContext(ctx, method, endpoint, r)
		if err != nil {
			return nil, err
		}

		c.addHeaders(req)
		if bodyBytes != nil {
			req.Header.Set("Content-Type", "application/json")
		}

		// Execute request (do() will handle 429 and return a sentinel error).
		resp, err := c.do(req)

		// If successful (no error), return immediately.
		if err == nil {
			return resp, nil
		}

		// Check if it's a rate-limit error (Code 5).
		if apiErr, ok := err.(*APIError); ok && apiErr.Code == 5 {
			// If we haven't exhausted retries, sleep and retry.
			if attempt < maxRetries {
				// Calculate backoff: min(base * 2^attempt, 30s) + jitter.
				delayMs := baseDelay * time.Duration(1<<uint(attempt))
				if delayMs > maxDelay {
					delayMs = maxDelay
				}
				jitter := time.Duration(rand.Int63n(1000)) * time.Millisecond
				totalDelay := delayMs + jitter

				// Check context before sleeping.
				select {
				case <-ctx.Done():
					return nil, ctx.Err()
				default:
				}

				time.Sleep(totalDelay)
				continue
			}
		}

		// Not a retryable error or max retries exhausted, return error.
		return resp, err
	}

	// Max retries exhausted on 429.
	return nil, &APIError{Code: 5, Message: "rate limited; max retries exceeded"}
}

// SetTenantID sets the X-Tenant-ID header sent on every request.
func (c *Client) SetTenantID(id string) {
	c.tenantID = id
}

func (c *Client) addHeaders(req *http.Request) {
	// Token injected via header — never via query param or URL.
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	if c.tenantID != "" {
		req.Header.Set("X-Tenant-ID", c.tenantID)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "gough-cli/1.0")
}

func (c *Client) do(req *http.Request) (*APIResponse, error) {
	// Log request if verbose mode is enabled.
	if c.logger != nil {
		logToken := c.maskedToken()
		c.logger.Debug("→ request", "method", req.Method, "url", req.URL.String(), "auth", logToken)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read body: %w", err)
	}

	// Log response if verbose mode is enabled.
	if c.logger != nil {
		c.logger.Debug("← response", "status", resp.StatusCode, "url", req.URL.String())
	}

	// Map HTTP status to exit-code sentinel errors.
	switch resp.StatusCode {
	case http.StatusUnauthorized:
		return nil, &APIError{Code: 3, Message: "insufficient scope or token expired; run `gough login`"}
	case http.StatusForbidden:
		var errBody struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(body, &errBody)
		if strings.Contains(errBody.Error, "tenant") {
			return nil, &APIError{Code: 4, Message: "tenant mismatch: " + errBody.Error}
		}
		return nil, &APIError{Code: 3, Message: "insufficient scope: " + errBody.Error}
	case http.StatusTooManyRequests:
		return nil, &APIError{Code: 5, Message: "rate limited; try again later"}
	case http.StatusUnprocessableEntity:
		return nil, &APIError{Code: 6, Message: parseErrorBody(body)}
	case http.StatusServiceUnavailable:
		return nil, &APIError{Code: 9, Message: "cluster unhealthy: " + parseErrorBody(body)}
	}

	var apiResp APIResponse
	if err := json.Unmarshal(body, &apiResp); err != nil {
		// Non-JSON response (e.g. proxy error).
		if resp.StatusCode >= 400 {
			return nil, &APIError{Code: 1, Message: fmt.Sprintf("HTTP %d: %s", resp.StatusCode, body)}
		}
		return nil, fmt.Errorf("decode response: %w", err)
	}

	if resp.StatusCode == http.StatusAccepted {
		// 202 Accepted: deferred operation.
		return &apiResp, nil
	}

	if resp.StatusCode >= 400 && apiResp.Error != "" {
		return nil, &APIError{Code: 1, Message: apiResp.Error}
	}

	return &apiResp, nil
}

// maskedToken returns the bearer token in masked form: "tok_****<last4>".
// If token is empty or too short, returns "tok_****".
func (c *Client) maskedToken() string {
	if len(c.token) < 4 {
		return "tok_****"
	}
	last4 := c.token[len(c.token)-4:]
	return "tok_****" + last4
}

// APIError carries both a human-readable message and an exit code.
type APIError struct {
	Code    int
	Message string
}

func (e *APIError) Error() string {
	return e.Message
}

// ExitCode returns the CLI exit code for this error.
func (e *APIError) ExitCode() int {
	return e.Code
}

func parseErrorBody(body []byte) string {
	var e struct {
		Error   string `json:"error"`
		Message string `json:"message"`
		Detail  string `json:"detail"`
	}
	_ = json.Unmarshal(body, &e)
	if e.Error != "" {
		return e.Error
	}
	if e.Message != "" {
		return e.Message
	}
	if e.Detail != "" {
		return e.Detail
	}
	return string(body)
}

// DecodeData unmarshals the Data field of an APIResponse into v.
func DecodeData(resp *APIResponse, v interface{}) error {
	if resp.Data == nil {
		return fmt.Errorf("response contained no data")
	}
	return json.Unmarshal(resp.Data, v)
}

// IsDeferred reports whether the response was a 202 deferred operation.
func IsDeferred(resp *APIResponse) bool {
	return resp.Status == "deferred" || resp.Note != ""
}

// ---- Domain-specific API methods ----

// Nodes

// NodeListParams holds query params for listing nodes.
type NodeListParams struct {
	State  string
	Tenant string
	Tag    string // key=value filter
	Page   int
	Size   int
}

// Node is the API representation of a cluster node.
type Node struct {
	ID             string            `json:"id"`
	Hostname       string            `json:"hostname"`
	State          string            `json:"state"`
	DMIUUID        string            `json:"dmi_uuid"`
	MgmtMAC        string            `json:"mgmt_mac"`
	CPUCount       int               `json:"cpu_count"`
	MemoryMB       int               `json:"memory_mb"`
	Arch           string            `json:"arch"`
	TenantID       string            `json:"tenant_id"`
	BiomeInstanceIDs []string          `json:"biome_instance_ids"`
	CreatedAt      string            `json:"created_at"`
	UpdatedAt      string            `json:"updated_at"`
	Tags           map[string]string `json:"tags"`
}

// ListNodes returns paginated nodes.
func (c *Client) ListNodes(ctx context.Context, p NodeListParams) ([]Node, error) {
	params := url.Values{}
	if p.State != "" {
		params.Set("state", p.State)
	}
	if p.Tenant != "" {
		params.Set("tenant_id", p.Tenant)
	}
	if p.Tag != "" {
		params.Set("tag", p.Tag)
	}
	if p.Page > 0 {
		params.Set("page", fmt.Sprint(p.Page))
	}
	if p.Size > 0 {
		params.Set("size", fmt.Sprint(p.Size))
	}
	resp, err := c.get(ctx, "/nodes", params)
	if err != nil {
		return nil, err
	}
	var nodes []Node
	return nodes, DecodeData(resp, &nodes)
}

// ShowNode returns a single node by ID.
func (c *Client) ShowNode(ctx context.Context, id string) (*Node, error) {
	resp, err := c.get(ctx, "/nodes/"+id, nil)
	if err != nil {
		return nil, err
	}
	var node Node
	return &node, DecodeData(resp, &node)
}

// DeployNode triggers Phase-1→2 plan compilation and deploy.
func (c *Client) DeployNode(ctx context.Context, id string, plan interface{}) (*APIResponse, error) {
	return c.post(ctx, "/nodes/"+id+"/deploy", plan)
}

// RejectNode rejects a probed node.
func (c *Client) RejectNode(ctx context.Context, id, reason string) (*APIResponse, error) {
	return c.post(ctx, "/nodes/"+id+"/reject", map[string]string{"reason": reason})
}

// DecommissionNode initiates node decommission.
func (c *Client) DecommissionNode(ctx context.Context, id, reason string) (*APIResponse, error) {
	return c.post(ctx, "/nodes/"+id+"/decommission", map[string]string{"reason": reason})
}

// EvacuateNode migrates all movable biomes off a node.
func (c *Client) EvacuateNode(ctx context.Context, id string) (*APIResponse, error) {
	return c.post(ctx, "/nodes/"+id+"/evacuate", nil)
}

// RekeyNode triggers LUKS key rotation for a node.
func (c *Client) RekeyNode(ctx context.Context, id, reason string) (*APIResponse, error) {
	return c.post(ctx, "/nodes/"+id+"/rekey", map[string]string{"reason": reason})
}

// AddNodeTag adds a key=value tag to a node.
func (c *Client) AddNodeTag(ctx context.Context, nodeID, key, value string) (*APIResponse, error) {
	return c.post(ctx, "/nodes/"+nodeID+"/tags", map[string]string{"key": key, "value": value})
}

// RemoveNodeTag removes a key=value tag from a node.
func (c *Client) RemoveNodeTag(ctx context.Context, nodeID, key, value string) (*APIResponse, error) {
	return c.del(ctx, "/nodes/"+nodeID+"/tags/"+key+"="+value)
}

// GetNodeTags returns all tags on a node.
func (c *Client) GetNodeTags(ctx context.Context, nodeID string) (map[string]string, error) {
	resp, err := c.get(ctx, "/nodes/"+nodeID+"/tags", nil)
	if err != nil {
		return nil, err
	}
	var tags map[string]string
	return tags, DecodeData(resp, &tags)
}

// Biomes

// Biome is the API representation of a biome catalog entry.
type Biome struct {
	Name             string `json:"name"`
	Version          string `json:"version"`
	Kind             string `json:"egg_kind"`
	Phase            string `json:"phase"`
	WorkloadType     string `json:"workload_type"`
	LockToHost       bool   `json:"lock_to_host"`
	UpgradeStrategy  string `json:"upgrade_strategy"`
	Description      string `json:"description"`
	PublishedAt      string `json:"published_at"`
	SigningKeyID     string `json:"signing_key_id"`
	SBOMUrl          string `json:"sbom_url"`
}

// ListBiomesParams holds filter params for listing biomes.
type ListBiomesParams struct {
	Kind     string
	Phase    string
	Workload string
}

// DeployBiomeParams holds parameters for deploying a biome to a node.
type DeployBiomeParams struct {
	BiomeName string            `json:"biome_name"`
	NodeID    string            `json:"node_id"`
	Params    map[string]string `json:"params,omitempty"`
}

// ListBiomes returns the biome catalog.
func (c *Client) ListBiomes(ctx context.Context, p ListBiomesParams) ([]Biome, error) {
	params := url.Values{}
	if p.Kind != "" {
		params.Set("biome_kind", p.Kind)
	}
	if p.Phase != "" {
		params.Set("phase", p.Phase)
	}
	if p.Workload != "" {
		params.Set("workload_type", p.Workload)
	}
	resp, err := c.get(ctx, "/biomes", params)
	if err != nil {
		return nil, err
	}
	var biomes []Biome
	return biomes, DecodeData(resp, &biomes)
}

// ShowBiome returns a biome by name, optionally at a specific version.
func (c *Client) ShowBiome(ctx context.Context, name, version string) (*Biome, error) {
	path := "/biomes/" + name
	params := url.Values{}
	if version != "" {
		params.Set("version", version)
	}
	resp, err := c.get(ctx, path, params)
	if err != nil {
		return nil, err
	}
	var biome Biome
	return &biome, DecodeData(resp, &biome)
}

// ValidateBiome runs static validation on the biome in the current directory.
func (c *Client) ValidateBiome(ctx context.Context, runTests bool) (*APIResponse, error) {
	return c.post(ctx, "/biomes/validate", map[string]bool{"run_tests": runTests})
}

// PublishBiome signs, generates SBOM, and pushes the biome.
func (c *Client) PublishBiome(ctx context.Context) (*APIResponse, error) {
	return c.post(ctx, "/biomes/publish", nil)
}

// PromoteBiome promotes a biome version.
func (c *Client) PromoteBiome(ctx context.Context, name, version string) (*APIResponse, error) {
	return c.post(ctx, "/biomes/"+name+"/promote", map[string]string{"version": version})
}

// RollbackBiome rolls back the named biome to the previous stable version.
func (c *Client) RollbackBiome(ctx context.Context, name string) (*APIResponse, error) {
	return c.post(ctx, "/biomes/"+name+"/rollback", nil)
}

// DiffBiomes returns a diff between two biome versions.
func (c *Client) DiffBiomes(ctx context.Context, name, v1, v2 string) (*APIResponse, error) {
	return c.get(ctx, "/biomes/"+name+"/diff",
		url.Values{"from": {v1}, "to": {v2}})
}

// ReSignBiome re-signs an existing biome version.
func (c *Client) ReSignBiome(ctx context.Context, name, version string) (*APIResponse, error) {
	return c.post(ctx, "/biomes/"+name+"/re-sign", map[string]string{"version": version})
}

// UpgradeBiome triggers rolling upgrade of biome instances to a target version.
func (c *Client) UpgradeBiome(ctx context.Context, name, toVersion string) (*APIResponse, error) {
	return c.post(ctx, "/biomes/"+name+"/upgrade", map[string]string{"to": toVersion})
}

// BiomeEligibilityCheck checks whether a biome instance is eligible for an operation.
func (c *Client) BiomeEligibilityCheck(ctx context.Context, biomeID string) (*APIResponse, error) {
	return c.get(ctx, "/biomes/instances/"+biomeID+"/eligibility", nil)
}

// DeployBiome deploys a biome to a node with optional parameters (e.g., control-plane frontend config).
func (c *Client) DeployBiome(ctx context.Context, p DeployBiomeParams) (*APIResponse, error) {
	return c.post(ctx, "/biomes/deploy", p)
}

// Cluster

// ClusterStatus is the summary of cluster health.
type ClusterStatus struct {
	ClusterID     string `json:"cluster_id"`
	State         string `json:"state"`
	NodeCount     int    `json:"node_count"`
	ReadyNodes    int    `json:"ready_nodes"`
	BiomeInstances  int    `json:"biome_instances"`
	Version       string `json:"version"`
	VaultSealed   bool   `json:"vault_sealed"`
	SpireHealthy  bool   `json:"spire_healthy"`
	DBHealthy     bool   `json:"db_healthy"`
	NATSHealthy   bool   `json:"nats_healthy"`
	K8SHealthy    bool   `json:"k8s_healthy"`
}

// GetClusterStatus returns the cluster status.
func (c *Client) GetClusterStatus(ctx context.Context) (*ClusterStatus, error) {
	resp, err := c.get(ctx, "/cluster/status", nil)
	if err != nil {
		return nil, err
	}
	var status ClusterStatus
	return &status, DecodeData(resp, &status)
}

// UpgradeCluster triggers a cluster-wide upgrade.
func (c *Client) UpgradeCluster(ctx context.Context) (*APIResponse, error) {
	return c.post(ctx, "/cluster/upgrade", nil)
}

// EvacuateCluster evacuates a specific node within the cluster.
func (c *Client) EvacuateCluster(ctx context.Context, nodeID string) (*APIResponse, error) {
	return c.post(ctx, "/cluster/evacuate", map[string]string{"node_id": nodeID})
}

// AdoptCluster adopts an existing k8s/lxd/ceph/longhorn resource.
func (c *Client) AdoptCluster(ctx context.Context, kind string, opts map[string]string) (*APIResponse, error) {
	body := map[string]interface{}{"kind": kind}
	for k, v := range opts {
		body[k] = v
	}
	return c.post(ctx, "/cluster/adopt", body)
}

// RotateJoinerSecrets rotates joiner secrets, optionally filtered by biome kind.
func (c *Client) RotateJoinerSecrets(ctx context.Context, biomeKind string) (*APIResponse, error) {
	body := map[string]string{}
	if biomeKind != "" {
		body["biome_kind"] = biomeKind
	}
	return c.post(ctx, "/cluster/rotate-joiner-secrets", body)
}

// LXDShowTrustPassword retrieves the LXD trust password (audit-logged).
func (c *Client) LXDShowTrustPassword(ctx context.Context, reason string) (*APIResponse, error) {
	return c.post(ctx, "/cluster/lxd/trust-password", map[string]string{"reason": reason})
}

// LXDRotateTrustPassword rotates the LXD cluster trust password.
func (c *Client) LXDRotateTrustPassword(ctx context.Context) (*APIResponse, error) {
	return c.post(ctx, "/cluster/lxd/trust-password/rotate", nil)
}

// IdentityPlaneStatus returns the current identity plane (SPIRE) status.
func (c *Client) IdentityPlaneStatus(ctx context.Context) (*APIResponse, error) {
	return c.get(ctx, "/cluster/identity-plane/status", nil)
}

// IdentityPlaneConfigure configures the identity plane provider.
func (c *Client) IdentityPlaneConfigure(ctx context.Context, provider string) (*APIResponse, error) {
	return c.post(ctx, "/cluster/identity-plane/configure", map[string]string{"provider": provider})
}

// NetworkBaselineStatus returns the current network baseline configuration.
func (c *Client) NetworkBaselineStatus(ctx context.Context) (*APIResponse, error) {
	return c.get(ctx, "/cluster/network-baseline/status", nil)
}

// NetworkBaselineConfigure configures a network baseline.
func (c *Client) NetworkBaselineConfigure(ctx context.Context, baseline string, opts map[string]string) (*APIResponse, error) {
	body := map[string]interface{}{"baseline": baseline}
	for k, v := range opts {
		body[k] = v
	}
	return c.post(ctx, "/cluster/network-baseline/configure", body)
}

// NetworkBaselineMigrate migrates a network baseline to a target provider (e.g. squawk).
func (c *Client) NetworkBaselineMigrate(ctx context.Context, baseline, to string) (*APIResponse, error) {
	return c.post(ctx, "/cluster/network-baseline/migrate", map[string]string{
		"baseline": baseline,
		"to":       to,
	})
}

// GetTagVocabulary returns the cluster tag vocabulary.
func (c *Client) GetTagVocabulary(ctx context.Context) (*APIResponse, error) {
	return c.get(ctx, "/cluster/tag-vocabulary", nil)
}

// Capacity & Migration

// CapacityForecast represents a capacity forecast.
type CapacityForecast struct {
	HorizonDays int             `json:"horizon_days"`
	Nodes       []NodeForecast  `json:"nodes"`
	GeneratedAt string          `json:"generated_at"`
}

// NodeForecast is the per-node forecast.
type NodeForecast struct {
	NodeID          string  `json:"node_id"`
	Hostname        string  `json:"hostname"`
	CPUUsedPct      float64 `json:"cpu_used_pct"`
	MemUsedPct      float64 `json:"mem_used_pct"`
	DiskUsedPct     float64 `json:"disk_used_pct"`
	ProjCPUPct      float64 `json:"projected_cpu_pct"`
	ProjMemPct      float64 `json:"projected_mem_pct"`
	BreachRisk      string  `json:"breach_risk"`
	BreachInDays    float64 `json:"breach_in_days,omitempty"`
}

// GetCapacityForecast fetches a capacity forecast.
func (c *Client) GetCapacityForecast(ctx context.Context, horizonDays int) (*CapacityForecast, error) {
	params := url.Values{"horizon_days": {fmt.Sprint(horizonDays)}}
	resp, err := c.get(ctx, "/capacity/forecast", params)
	if err != nil {
		return nil, err
	}
	var fc CapacityForecast
	return &fc, DecodeData(resp, &fc)
}

// GetCapacityRisks returns identified capacity risks.
func (c *Client) GetCapacityRisks(ctx context.Context) (*APIResponse, error) {
	return c.get(ctx, "/capacity/risks", nil)
}

// MigrationPolicy holds migration safety policy fields.
type MigrationPolicy struct {
	MinHealthyNodes                   int  `json:"min_healthy_nodes"`
	MaxConcurrentMigrations           int  `json:"max_concurrent_migrations"`
	RequireTargetCapacityHeadroomMemPct int `json:"require_target_capacity_headroom_mem_pct"`
	RequireTargetCapacityHeadroomCPUPct int `json:"require_target_capacity_headroom_cpu_pct"`
	RollbackOnDestinationFailure      bool `json:"rollback_on_destination_failure"`
}

// GetMigrationPolicy returns the migration safety policy.
func (c *Client) GetMigrationPolicy(ctx context.Context) (*MigrationPolicy, error) {
	resp, err := c.get(ctx, "/migration/policy", nil)
	if err != nil {
		return nil, err
	}
	var p MigrationPolicy
	return &p, DecodeData(resp, &p)
}

// SetMigrationPolicy updates a single field in the migration policy.
func (c *Client) SetMigrationPolicy(ctx context.Context, field, value string) (*APIResponse, error) {
	return c.patch(ctx, "/migration/policy", map[string]string{field: value})
}

// TriggerMigration triggers manual migration for a biome instance.
func (c *Client) TriggerMigration(ctx context.Context, biomeInstanceID, targetNodeID string, ignoreLock bool, reason string) (*APIResponse, error) {
	body := map[string]interface{}{
		"biome_instance_id": biomeInstanceID,
		"ignore_lock":     ignoreLock,
	}
	if targetNodeID != "" {
		body["target_node_id"] = targetNodeID
	}
	if reason != "" {
		body["reason"] = reason
	}
	return c.post(ctx, "/migration/trigger", body)
}

// DR

// RunDRDrill triggers a DR drill.
func (c *Client) RunDRDrill(ctx context.Context, target string) (*APIResponse, error) {
	body := map[string]string{}
	if target != "" {
		body["target"] = target
	}
	return c.post(ctx, "/dr/drill", body)
}

// PromoteDRSite promotes a DR site.
func (c *Client) PromoteDRSite(ctx context.Context, siteID, reason string, sourceUnreachable bool) (*APIResponse, error) {
	return c.post(ctx, "/dr/promote", map[string]interface{}{
		"site_id":           siteID,
		"reason":            reason,
		"source_unreachable": sourceUnreachable,
	})
}

// FailbackDRSite fails back to the primary site.
func (c *Client) FailbackDRSite(ctx context.Context, siteID string) (*APIResponse, error) {
	return c.post(ctx, "/dr/failback", map[string]string{"site_id": siteID})
}

// RestoreFromBackup triggers a restore from an S3 backup.
func (c *Client) RestoreFromBackup(ctx context.Context, s3URL, clusterID string) (*APIResponse, error) {
	return c.post(ctx, "/dr/restore", map[string]string{
		"from":       s3URL,
		"cluster_id": clusterID,
	})
}

// Audit

// AuditEvent is a single audit log entry.
type AuditEvent struct {
	ID        string          `json:"id"`
	Timestamp string          `json:"timestamp"`
	Actor     string          `json:"actor"`
	Action    string          `json:"action"`
	Resource  string          `json:"resource"`
	TenantID  string          `json:"tenant_id"`
	Data      json.RawMessage `json:"data"`
}

// ListAuditEvents returns paginated audit events.
func (c *Client) ListAuditEvents(ctx context.Context, since, tenantID string) ([]AuditEvent, error) {
	params := url.Values{}
	if since != "" {
		params.Set("since", since)
	}
	if tenantID != "" {
		params.Set("tenant_id", tenantID)
	}
	resp, err := c.get(ctx, "/audit", params)
	if err != nil {
		return nil, err
	}
	var events []AuditEvent
	return events, DecodeData(resp, &events)
}

// VerifyAuditChain verifies the audit hash chain for the given time range.
func (c *Client) VerifyAuditChain(ctx context.Context, since, to string) (*APIResponse, error) {
	params := url.Values{}
	if since != "" {
		params.Set("since", since)
	}
	if to != "" {
		params.Set("to", to)
	}
	return c.get(ctx, "/audit/verify", params)
}

// ExportAuditLog exports audit events to a jsonl file.
func (c *Client) ExportAuditLog(ctx context.Context, since, output string) (*APIResponse, error) {
	return c.post(ctx, "/audit/export", map[string]string{
		"since":  since,
		"output": output,
	})
}

// Vault

// VaultUnseal submits a Shamir unseal share.
func (c *Client) VaultUnseal(ctx context.Context, share string) (*APIResponse, error) {
	// share is sensitive — handled opaquely; never logged.
	return c.post(ctx, "/vault/unseal", map[string]string{"share": share})
}

// Primary

// ReplacePrimary replaces the primary node.
func (c *Client) ReplacePrimary(ctx context.Context, nodeID string) (*APIResponse, error) {
	return c.post(ctx, "/primary/replace", map[string]string{"node_id": nodeID})
}

// ForceRecoverPrimary force-recovers the primary using a surviving node.
func (c *Client) ForceRecoverPrimary(ctx context.Context, survivingNodeID, reason string) (*APIResponse, error) {
	return c.post(ctx, "/primary/force-recover", map[string]string{
		"surviving_node_id": survivingNodeID,
		"reason":            reason,
	})
}

// SwitchPrimaryFrontend switches the primary frontend mode (vip|anycast).
func (c *Client) SwitchPrimaryFrontend(ctx context.Context, mode string) (*APIResponse, error) {
	return c.post(ctx, "/primary/frontend-switch", map[string]string{"mode": mode})
}

// RotatePrimaryCA rotates the internal CA.
func (c *Client) RotatePrimaryCA(ctx context.Context) (*APIResponse, error) {
	return c.post(ctx, "/primary/rotate-ca", nil)
}

// Disks

// DiskInfo represents a disk on a node.
type DiskInfo struct {
	ID         string `json:"id"`
	NodeID     string `json:"node_id"`
	Device     string `json:"device"`
	SizeGB     int    `json:"size_gb"`
	Kind       string `json:"kind"`
	DarkDrive  bool   `json:"dark_drive"`
	SmartState string `json:"smart_state"`
	Model      string `json:"model"`
	Serial     string `json:"serial"`
}

// ListDisks returns disks for a node.
func (c *Client) ListDisks(ctx context.Context, nodeID string) ([]DiskInfo, error) {
	resp, err := c.get(ctx, "/nodes/"+nodeID+"/disks", nil)
	if err != nil {
		return nil, err
	}
	var disks []DiskInfo
	return disks, DecodeData(resp, &disks)
}

// PlanDisks applies a disk plan to a node.
func (c *Client) PlanDisks(ctx context.Context, nodeID string, plan interface{}) (*APIResponse, error) {
	return c.post(ctx, "/nodes/"+nodeID+"/disk-plan", plan)
}

// SmartRecheck triggers a SMART re-check for a specific disk.
func (c *Client) SmartRecheck(ctx context.Context, nodeID, diskID string) (*APIResponse, error) {
	return c.post(ctx, "/nodes/"+nodeID+"/disks/"+diskID+"/smart-recheck", nil)
}

// Sync (air-gap)

// SyncExport triggers a bundle export.
func (c *Client) SyncExport(ctx context.Context, dir string) (*APIResponse, error) {
	return c.post(ctx, "/sync/export", map[string]string{"dir": dir})
}

// SyncImport applies a bundle to the air-gapped primary.
func (c *Client) SyncImport(ctx context.Context, dir string) (*APIResponse, error) {
	return c.post(ctx, "/sync/import", map[string]string{"dir": dir})
}

// Doctor / Health

// RunDoctorCheck runs a named doctor check.
func (c *Client) RunDoctorCheck(ctx context.Context, check string, params map[string]string) (*APIResponse, error) {
	body := map[string]interface{}{"check": check}
	for k, v := range params {
		body[k] = v
	}
	return c.post(ctx, "/doctor/"+check, body)
}

// Integrations

// Integration represents an external integration.
type Integration struct {
	Name      string `json:"name"`
	Status    string `json:"status"`
	Version   string `json:"version"`
	Endpoint  string `json:"endpoint"`
	LastPing  string `json:"last_ping"`
	ScopeOK   bool   `json:"scope_ok"`
}

// ListIntegrations returns all configured integrations.
func (c *Client) ListIntegrations(ctx context.Context) ([]Integration, error) {
	resp, err := c.get(ctx, "/integrations", nil)
	if err != nil {
		return nil, err
	}
	var integrations []Integration
	return integrations, DecodeData(resp, &integrations)
}

// ConfigureIntegration configures a named integration.
func (c *Client) ConfigureIntegration(ctx context.Context, name string, cfg map[string]string) (*APIResponse, error) {
	return c.post(ctx, "/integrations/"+name+"/configure", cfg)
}

// RotateIntegrationCredentials rotates credentials for a named integration.
func (c *Client) RotateIntegrationCredentials(ctx context.Context, name string) (*APIResponse, error) {
	return c.post(ctx, "/integrations/"+name+"/rotate-credentials", nil)
}

// ValidateIntegrationScope validates scopes for a named integration.
func (c *Client) ValidateIntegrationScope(ctx context.Context, name string) (*APIResponse, error) {
	return c.post(ctx, "/integrations/"+name+"/validate-scope", nil)
}

// Webhooks

// TestWebhook sends a test event to a webhook.
func (c *Client) TestWebhook(ctx context.Context, webhookID string) (*APIResponse, error) {
	return c.post(ctx, "/webhooks/"+webhookID+"/test", nil)
}

// Dev

// NodeSimParams holds node-sim launch parameters.
type NodeSimParams struct {
	Arch     string `json:"arch"`
	Firmware string `json:"firmware"`
	IPv6     bool   `json:"ipv6"`
}

// StartNodeSim starts the developer node simulator.
func (c *Client) StartNodeSim(ctx context.Context, p NodeSimParams) (*APIResponse, error) {
	return c.post(ctx, "/dev/node-sim", p)
}

// DevSeed seeds a dev cluster with the specified biomes.
func (c *Client) DevSeed(ctx context.Context, biomes string) (*APIResponse, error) {
	return c.post(ctx, "/dev/seed", map[string]string{"biomes": biomes})
}

// DevReset resets the dev cluster to a clean state.
func (c *Client) DevReset(ctx context.Context) (*APIResponse, error) {
	return c.post(ctx, "/dev/reset", nil)
}

// Gough Init

// InitParams holds parameters for gough init.
type InitParams struct {
	HA               bool   `json:"ha"`
	DHCPAuthoritative bool   `json:"dhcp_authoritative"`
	RestoreFrom      string `json:"restore_from,omitempty"`
}

// Init bootstraps the primary node.
func (c *Client) Init(ctx context.Context, p InitParams) (*APIResponse, error) {
	return c.post(ctx, "/init", p)
}

// Deployments

// Deployment represents a deployment record.
type Deployment struct {
	ID           string `json:"id"`
	BiomeID      int    `json:"biome_id"`
	NodeID       int    `json:"node_id"`
	BiomeName    string `json:"biome_name"`
	Status       string `json:"status"`
	Phase        int    `json:"phase"`
	LogsURL      string `json:"logs_url"`
	ErrorMessage string `json:"error_message"`
	CreatedAt    string `json:"created_at"`
	UpdatedAt    string `json:"updated_at"`
}

// DeploymentLog represents a single deployment log entry.
type DeploymentLog struct {
	ID           string `json:"id"`
	DeploymentID string `json:"deployment_id"`
	Message      string `json:"message"`
	Level        string `json:"level"`
	CreatedAt    string `json:"created_at"`
}

// DeploymentListParams holds optional filters for listing deployments.
type DeploymentListParams struct {
	Status   string
	BiomeID  string
	NodeID   string
}

// ListDeployments returns deployments filtered by optional params.
func (c *Client) ListDeployments(ctx context.Context, p DeploymentListParams) ([]Deployment, error) {
	params := url.Values{}
	if p.Status != "" {
		params.Set("status", p.Status)
	}
	if p.BiomeID != "" {
		params.Set("biome_id", p.BiomeID)
	}
	if p.NodeID != "" {
		params.Set("node_id", p.NodeID)
	}
	resp, err := c.get(ctx, "/deployments", params)
	if err != nil {
		return nil, err
	}
	var deployments []Deployment
	return deployments, DecodeData(resp, &deployments)
}

// GetDeploymentLogs returns logs for a deployment.
func (c *Client) GetDeploymentLogs(ctx context.Context, deploymentID string, tail int) ([]DeploymentLog, error) {
	params := url.Values{}
	if tail > 0 {
		params.Set("tail", fmt.Sprintf("%d", tail))
	}
	resp, err := c.get(ctx, "/deployments/"+deploymentID+"/logs", params)
	if err != nil {
		return nil, err
	}
	var logs []DeploymentLog
	return logs, DecodeData(resp, &logs)
}

// CancelDeployment cancels a pending or in-progress deployment.
func (c *Client) CancelDeployment(ctx context.Context, deploymentID string) (*APIResponse, error) {
	return c.post(ctx, "/deployments/"+deploymentID+"/cancel", nil)
}

// Storage Quotas

// StorageQuota represents a storage quota record.
type StorageQuota struct {
	ID           string  `json:"id"`
	TenantID     string  `json:"tenant_id"`
	ResourceType string  `json:"resource_type"`
	LimitValue   float64 `json:"limit_value"`
	UsedValue    float64 `json:"used_value"`
	Unit         string  `json:"unit"`
	UpdatedAt    string  `json:"updated_at"`
}

// StorageQuotaRequestParams holds parameters for a quota request.
type StorageQuotaRequestParams struct {
	TenantID       string  `json:"tenant_id"`
	ResourceType   string  `json:"resource_type"`
	RequestedValue float64 `json:"requested_value"`
	Unit           string  `json:"unit"`
	Justification  string  `json:"justification"`
}

// ListStorageQuotas returns storage quotas for a tenant.
func (c *Client) ListStorageQuotas(ctx context.Context, tenantID string) ([]StorageQuota, error) {
	params := url.Values{}
	if tenantID != "" {
		params.Set("tenant_id", tenantID)
	}
	resp, err := c.get(ctx, "/storage/quotas", params)
	if err != nil {
		return nil, err
	}
	var quotas []StorageQuota
	return quotas, DecodeData(resp, &quotas)
}

// RequestStorageQuota submits a storage quota increase request.
func (c *Client) RequestStorageQuota(ctx context.Context, p StorageQuotaRequestParams) (*APIResponse, error) {
	return c.post(ctx, "/storage/quota-request", p)
}

// Meta / Version

// GetClientVersion returns the recommended client version from the cluster.
func (c *Client) GetClientVersion(ctx context.Context) (*APIResponse, error) {
	return c.get(ctx, "/meta/version", nil)
}
