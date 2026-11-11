"""Centralized OIDC scope policy for Gough API Manager."""

from __future__ import annotations

import re

# ==============================================================================
# Known Scopes Catalog
# ==============================================================================

KNOWN_SCOPES: frozenset[str] = frozenset({
    # Read scopes (cluster, infrastructure introspection)
    "gough.cluster.read",
    "gough.nodes.read",
    "gough.disks.read",
    "gough.biomes.read",
    "gough.capacity.read",
    "gough.storage.read",
    "gough.audit.read",
    "gough.dr.read",
    "gough.bmc.read",
    "gough.joiner.read",

    # Write scopes (provisioning, configuration)
    "gough.nodes.provision",
    "gough.nodes.rekey",
    "gough.nodes.decommission",
    "gough.disks.plan",
    "gough.biomes.author",
    "gough.biomes.deploy",
    "gough.biomes.sign",
    "gough.storage.configure",
    "gough.migration.policy",
    "gough.migration.trigger",
    "gough.dr.drill",
    "gough.bmc.configure",
    "gough.joiner.rotate",

    # Admin and superadmin scopes (complete system control)
    "gough.cluster.admin",
    "gough.cluster.superadmin",

    # Dangerous override scopes (require explicit approval/MFA in Wave 2)
    "gough.dr.promote",
    "gough.biomes.unsafe-skip-signing",
    "gough.migration.override-lock",
})


# ==============================================================================
# Scope Policy: (HTTP Method, Path Pattern) -> Required Scopes Set
# ==============================================================================

SCOPE_POLICY: dict[tuple[str, str], frozenset[str] | None] = {
    # Biomes endpoints (existing in Sprint 1 M1)
    ("GET", "/api/v1/biomes"): frozenset({"gough.biomes.read"}),
    ("POST", "/api/v1/biomes"): frozenset({"gough.biomes.author"}),
    ("GET", "/api/v1/biomes/<int:biome_id>"): frozenset({"gough.biomes.read"}),
    ("PUT", "/api/v1/biomes/<int:biome_id>"): frozenset({"gough.biomes.author"}),
    ("DELETE", "/api/v1/biomes/<int:biome_id>"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/biomes/<int:biome_id>/upload"): frozenset({"gough.biomes.author"}),
    ("POST", "/api/v1/biomes/<int:biome_id>/sign"): frozenset({"gough.biomes.sign"}),
    ("POST", "/api/v1/biomes/<int:biome_id>/upgrade"): frozenset({"gough.biomes.deploy"}),
    ("GET", "/api/v1/biomes/<int:biome_id>/eligibility"): frozenset({"gough.biomes.read"}),
    ("POST", "/api/v1/biomes/render-cloud-init"): frozenset({"gough.biomes.read"}),
    ("GET", "/api/v1/biomes/groups"): frozenset({"gough.biomes.read"}),
    ("POST", "/api/v1/biomes/groups"): frozenset({"gough.biomes.author"}),
    ("GET", "/api/v1/biomes/groups/<int:group_id>"): frozenset({"gough.biomes.read"}),
    ("PUT", "/api/v1/biomes/groups/<int:group_id>"): frozenset({"gough.biomes.author"}),
    ("DELETE", "/api/v1/biomes/groups/<int:group_id>"): frozenset({"gough.cluster.admin"}),

    # Nodes endpoints
    ("GET", "/api/v1/nodes"): frozenset({"gough.nodes.read"}),
    ("GET", "/api/v1/nodes/<int:node_id>"): frozenset({"gough.nodes.read"}),
    ("PATCH", "/api/v1/nodes/<int:node_id>"): frozenset({"gough.nodes.provision"}),
    ("POST", "/api/v1/nodes/<int:node_id>/deploy"): frozenset({"gough.nodes.provision"}),
    ("POST", "/api/v1/nodes/<int:node_id>/reject"): frozenset({"gough.nodes.provision"}),
    ("POST", "/api/v1/nodes/<int:node_id>/evacuate"): frozenset({"gough.nodes.provision"}),
    ("DELETE", "/api/v1/nodes/<int:node_id>"): frozenset({"gough.nodes.decommission"}),
    # POST /nodes/<id>/events authenticates with a service SVID (mTLS) inside
    # the handler, not an OIDC bearer -> ANONYMOUS_PATHS (see below).
    ("GET", "/api/v1/nodes/<int:node_id>/tags"): frozenset({"gough.nodes.read"}),
    ("PATCH", "/api/v1/nodes/<int:node_id>/tags"): frozenset({"gough.nodes.provision"}),
    # POST /nodes/discover authenticates with a one-time bootstrap token (HS256)
    # validated inside the handler, not an OIDC bearer -> ANONYMOUS_PATHS (below).
    ("POST", "/api/v1/nodes/<int:node_id>/biomes"): frozenset({"gough.biomes.deploy"}),
    ("GET", "/api/v1/nodes/<int:node_id>/biomes"): frozenset({"gough.biomes.read"}),
    ("DELETE", "/api/v1/nodes/<int:node_id>/biomes/<int:biome_id>"): frozenset({"gough.biomes.deploy"}),

    # Disks endpoints
    ("GET", "/api/v1/nodes/<int:node_id>/disks"): frozenset({"gough.disks.read"}),
    ("POST", "/api/v1/nodes/<int:node_id>/disks/<int:disk_id>/plan"): frozenset({"gough.disks.plan"}),
    ("PATCH", "/api/v1/nodes/<int:node_id>/disks/<int:disk_id>"): frozenset({"gough.disks.plan"}),
    ("POST", "/api/v1/nodes/<int:node_id>/disks/<int:disk_id>/smart-recheck"): frozenset({"gough.disks.plan"}),

    # Migration endpoints
    ("GET", "/api/v1/migration/policy"): frozenset({"gough.capacity.read"}),
    ("PATCH", "/api/v1/migration/policy"): frozenset({"gough.migration.policy"}),
    ("POST", "/api/v1/migration/biome/<int:instance_id>"): frozenset({"gough.migration.trigger"}),
    ("GET", "/api/v1/migration/events"): frozenset({"gough.capacity.read"}),
    ("GET", "/api/v1/migration/safety-envelope"): frozenset({"gough.capacity.read"}),

    # Capacity endpoints
    ("GET", "/api/v1/capacity/forecast"): frozenset({"gough.capacity.read"}),
    ("GET", "/api/v1/capacity/risks"): frozenset({"gough.capacity.read"}),

    # Audit endpoints
    ("GET", "/api/v1/audit/events"): frozenset({"gough.audit.read"}),
    ("POST", "/api/v1/audit/verify"): frozenset({"gough.audit.read"}),
    ("GET", "/api/v1/audit/export"): frozenset({"gough.cluster.superadmin"}),

    # Joiner secrets endpoints
    ("GET", "/api/v1/clusters/<uuid:cluster_id>/joiner-secrets"): frozenset({"gough.joiner.read"}),
    ("POST", "/api/v1/clusters/<uuid:cluster_id>/joiner-secrets/<uuid:js_id>/rotate"): frozenset({"gough.joiner.rotate"}),
    ("DELETE", "/api/v1/clusters/<uuid:cluster_id>/joiner-secrets/<uuid:js_id>"): frozenset({"gough.cluster.admin"}),

    # Cluster storage endpoints
    ("GET", "/api/v1/clusters/<uuid:cluster_id>/storage"): frozenset({"gough.storage.read"}),
    ("PATCH", "/api/v1/clusters/<uuid:cluster_id>/storage"): frozenset({"gough.storage.configure"}),
    ("POST", "/api/v1/clusters/<uuid:cluster_id>/storage/switch-primary"): frozenset({"gough.cluster.admin"}),

    # Cluster LXD endpoints
    ("GET", "/api/v1/clusters/<uuid:cluster_id>/lxd/members"): frozenset({"gough.cluster.read"}),
    ("POST", "/api/v1/clusters/<uuid:cluster_id>/lxd/join"): frozenset({"gough.cluster.admin"}),

    # Cluster network endpoints
    ("GET", "/api/v1/clusters/<uuid:cluster_id>/network-pools"): frozenset({"gough.cluster.read"}),
    ("PATCH", "/api/v1/clusters/<uuid:cluster_id>/network-pools"): frozenset({"gough.cluster.admin"}),

    # Cluster identity plane endpoints
    ("GET", "/api/v1/clusters/<uuid:cluster_id>/identity-plane"): frozenset({"gough.cluster.read"}),
    ("PATCH", "/api/v1/clusters/<uuid:cluster_id>/identity-plane"): frozenset({"gough.cluster.admin"}),

    # Cluster adoption and config endpoints
    ("POST", "/api/v1/clusters/<uuid:cluster_id>/adopt"): frozenset({"gough.cluster.superadmin"}),
    ("GET", "/api/v1/clusters/<uuid:cluster_id>/config"): frozenset({"gough.cluster.read"}),
    ("PATCH", "/api/v1/clusters/<uuid:cluster_id>/config"): frozenset({"gough.cluster.admin"}),

    # Primary HA endpoints
    ("GET", "/api/v1/primary/status"): frozenset({"gough.cluster.read"}),
    ("POST", "/api/v1/primary/replace"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/primary/force-recover"): frozenset({"gough.cluster.superadmin"}),

    # Webhooks endpoints (real rules use a bare <webhook_id> converter; the
    # former duplicate <int:...>/<string:...> blocks collapsed to one).
    ("GET", "/api/v1/webhooks"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/webhooks"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/webhooks/<webhook_id>"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/webhooks/<webhook_id>/test"): frozenset({"gough.cluster.admin"}),
    # GET /webhooks/keys/<tenant> serves a PUBLIC JWKS -> ANONYMOUS_PATHS (below).

    # Integrations endpoints
    ("GET", "/api/v1/integrations/status"): frozenset({"gough.cluster.read"}),
    ("POST", "/api/v1/integrations/<string:product>/configure"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/integrations/<string:product>/rotate-credentials"): frozenset({"gough.cluster.admin"}),

    # iPXE endpoints
    ("POST", "/api/v1/ipxe/bind-mac"): frozenset({"gough.nodes.provision"}),
    ("POST", "/api/v1/ipxe/mint-bootstrap-token"): frozenset({"gough.nodes.provision"}),

    # Authentication endpoints (regression: gh-31 Bug 3)
    # These require authentication but no specific scopes (any valid user can call them)
    ("GET", "/api/v1/auth/me"): frozenset(),
    ("POST", "/api/v1/auth/logout"): frozenset(),
    ("POST", "/api/v1/auth/change-password"): frozenset(),

    # OpenAPI spec endpoints (regression: audit openapi-anon 2026-09-22).
    # Require authentication but no specific scope -- any valid token may
    # read the API surface documentation.
    ("GET", "/api/v1/openapi.json"): frozenset(),
    ("GET", "/api/v1/openapi.yaml"): frozenset(),
    # ------------------------------------------------------------------
    # Reviewed mappings (security review of 385dd0f). Each entry below was
    # checked in its handler; routes that failed review are NOT here -- they
    # are listed in INTENTIONALLY_UNMAPPED with the fix they wait on.
    # The route | decision | reason | evidence table is in the PR description.
    # ------------------------------------------------------------------

    # Superadmin only: global (non-tenant) stores and CA / control-plane ops.
    # A tenant ``admin`` (gough.cluster.admin) must never reach these: the
    # user store has no tenant filter (password reset / role grant on any
    # account) and the secrets store is global with caller-chosen paths.
    ("GET", "/api/v1/users"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/users"): frozenset({"gough.cluster.superadmin"}),
    ("GET", "/api/v1/users/roles"): frozenset({"gough.cluster.superadmin"}),
    ("GET", "/api/v1/users/<int:user_id>"): frozenset({"gough.cluster.superadmin"}),
    ("PUT", "/api/v1/users/<int:user_id>"): frozenset({"gough.cluster.superadmin"}),
    ("DELETE", "/api/v1/users/<int:user_id>"): frozenset({"gough.cluster.superadmin"}),
    ("GET", "/api/v1/secrets"): frozenset({"gough.cluster.superadmin"}),
    ("GET", "/api/v1/secrets/backends"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/secrets/backends"): frozenset({"gough.cluster.superadmin"}),
    ("GET", "/api/v1/secrets/<path:secret_path>"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/secrets/<path:secret_path>"): frozenset({"gough.cluster.superadmin"}),
    ("PUT", "/api/v1/secrets/<path:secret_path>"): frozenset({"gough.cluster.superadmin"}),
    ("DELETE", "/api/v1/secrets/<path:secret_path>"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/ssh-ca/initialize"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/primary/rotate-ca"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/primary/frontend-switch"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/vault/rotate-keys"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/integrations/<product>/compromise-response"): frozenset({"gough.cluster.superadmin"}),

    # SSH CA: public key is non-secret; sign is admin-scoped AND the handler
    # caps validity (<=3600 s), enforces a principal allowlist (root never),
    # and binds the certificate to one in-tenant node.
    ("GET", "/api/v1/ssh-ca/public-key"): frozenset({"gough.cluster.read"}),
    ("POST", "/api/v1/ssh-ca/sign"): frozenset({"gough.cluster.admin"}),

    # iPXE boot configuration WRITES: gough.cluster.superadmin. These rows
    # (ipxe_config, boot configs, images, elder_config) are platform-wide with
    # no tenant_id, so a tenant admin (gough.cluster.admin) changing them
    # would rewrite every tenant's boot chain. Delegating them to tenant
    # admins needs a tenant-scoped iPXE model first (follow-up). Handlers
    # repeat the superadmin check, type-check input, vet the Elder URL
    # against the Elder egress policy (https, public-only unless
    # allowlisted), return allow-listed DTOs and audit-log every change. The
    # matching GET routes stay unmapped (raw row echo) -- see
    # INTENTIONALLY_UNMAPPED. Regression: security review of PR #52.
    ("PUT", "/api/v1/ipxe/config"): frozenset({"gough.cluster.superadmin"}),
    ("PUT", "/api/v1/ipxe/elder/config"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/ipxe/boot-configs"): frozenset({"gough.cluster.superadmin"}),
    ("PUT", "/api/v1/ipxe/boot-configs/<int:config_id>"): frozenset({"gough.cluster.superadmin"}),
    ("DELETE", "/api/v1/ipxe/boot-configs/<int:config_id>"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/ipxe/images"): frozenset({"gough.cluster.superadmin"}),
    ("PUT", "/api/v1/ipxe/images/<int:image_id>"): frozenset({"gough.cluster.superadmin"}),
    ("DELETE", "/api/v1/ipxe/images/<int:image_id>"): frozenset({"gough.cluster.superadmin"}),

    # Tenant-scoped handlers that passed review (tenant guard + validation +
    # explicit response shape).
    ("GET", "/api/v1/clusters/<cluster_id>/lxd/status"): frozenset({"gough.cluster.read"}),
    ("GET", "/api/v1/clusters/<cluster_id>/network-baseline-topology"): frozenset({"gough.cluster.read"}),
    ("PATCH", "/api/v1/clusters/<cluster_id>/network-baseline-topology"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/nodes/<int:node_id>/cloud-init"): frozenset({"gough.nodes.read"}),

    # Deployment-global integration status (no tenant data; product name
    # validated; credentials returned only via _safe_credentials_view).
    ("GET", "/api/v1/integrations/<product>"): frozenset({"gough.cluster.read"}),
    ("POST", "/api/v1/integrations/<product>/validate-scope"): frozenset({"gough.cluster.admin"}),

    # Example routes: any authenticated principal / read scope. Unused by any
    # client -- candidates for removal.
    ("GET", "/api/v1/hello"): frozenset(),
    ("GET", "/api/v1/hello/protected"): frozenset({"gough.cluster.read"}),
}


# ==============================================================================
# Anonymous Paths (No Authentication Required)
# ==============================================================================

ANONYMOUS_PATHS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/health"),
    ("GET", "/healthz"),
    ("GET", "/ready"),
    ("GET", "/readyz"),
    ("GET", "/metrics"),
    ("GET", "/api/v1/version"),
    # openapi.json/.yaml are NOT anonymous (regression: audit openapi-anon
    # 2026-09-22) -- the full 176-route spec is a reconnaissance map and
    # requires a valid bearer token; see SCOPE_POLICY below (registered with
    # an empty required-scope set: any authenticated principal, no specific
    # scope needed).
    ("GET", "/api/v1/ipxe/helper/<string:mac>"),
    ("GET", "/api/v1/ipxe/deploy/<string:mac>"),
    # /ipxe/kernel, /ipxe/initrd and /ipxe/helper-efi entries were removed:
    # no such routes exist in this service (artifacts are served from the
    # primary's artifact URL). A dead anonymous entry would silently make any
    # future route at that path unauthenticated.
    # Authentication endpoints (regression: gh-31 and gh-31 Bug 3)
    # login, refresh: entry points (no JWT required)
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),
    # Password reset: public (forgot-password flow has no JWT; reset_token in body)
    ("POST", "/api/v1/auth/request-password-reset"),
    ("POST", "/api/v1/auth/reset-password"),
    # Public status endpoint (hello.status; no auth by design).
    ("GET", "/api/v1/status"),
    # Auth service liveness probe (no secrets; mirrors /health).
    ("GET", "/api/v1/auth/health"),
    # Endpoints that authenticate with a NON-OIDC scheme inside the handler
    # (regression: gh-31 -- the ES256 ASGI gate must skip these or their real
    # callers can never reach the handler that runs their own auth check).
    # Agent enrollment/token endpoints (X-Enrollment-Key header / HS256 agent
    # access+refresh tokens, all validated in app.api.agents).
    ("POST", "/api/v1/agents/enroll"),
    ("POST", "/api/v1/agents/refresh"),
    ("POST", "/api/v1/agents/heartbeat"),
    # Node discovery: one-time bootstrap token (HS256) validated in the handler.
    ("POST", "/api/v1/nodes/discover"),
    # Node events: service SVID (mTLS X.509) validated in the handler.
    ("POST", "/api/v1/nodes/<int:node_id>/events"),
    # Public webhook JWKS (verification keys for a tenant).
    ("GET", "/api/v1/webhooks/keys/<string:tenant>"),
})


# ==============================================================================
# Intentionally Unmapped (fail-closed 403 pending a handler fix)
# ==============================================================================
#
# Real routes deliberately left OUT of SCOPE_POLICY, so the fail-closed
# scope middleware answers 403 for every caller (superadmin included) until
# the named follow-up lands. The route-coverage gate
# (tests/security/test_scope_policy_route_coverage.py) requires every real
# route to be in SCOPE_POLICY, ANONYMOUS_PATHS or here, requires each reason
# to name its follow-up, and fails on entries that match no real route.
# Mapping one of these means fixing the handler first, then moving the entry.

_FU_STORAGE_TENANT = (
    "Follow-up: bind storage_config/bucket/object operations to the JWT tenant "
    "(storage_config has no tenant_id; bucket/config chosen by the caller)."
)
_FU_STORAGE_QUOTA = (
    "Follow-up: take tenant from the validated JWT, never a query/body param "
    "(quotas lists every tenant when the param is absent)."
)
_FU_TEAMS = (
    "Follow-up: tenant-scope teams/members/resources queries and replace int-id "
    "lookups with tenant-checked public_id UUIDs (current int-id IDOR)."
)
_FU_CLOUDS = (
    "Follow-up: tenant-scope cloud_providers queries and replace int-id lookups "
    "with tenant-checked public_id UUIDs (current int-id IDOR)."
)
_FU_IPXE_READ = (
    "Follow-up: replace raw as_dict()/to_dict() echo with allow-listed DTOs "
    "(bmc_address and internal fields leak) and decide tenancy of boot config."
)
_FU_IPXE_MACHINES = (
    "Follow-up: ipxe_machines has no tenant_id; add tenant binding plus "
    "allow-listed DTOs before any machine read or lifecycle action is mapped."
)
_FU_AGENTS = (
    "Follow-up: access agents/enrollment keys are global with no tenant column; "
    "decide superadmin-only vs tenant binding, then add DTOs."
)
_FU_SHELL = (
    "Follow-up: shell sessions branch on the role name 'admin', are not "
    "tenant-scoped, and list_sessions' ended_at filter is a no-op ('is None')."
)
_FU_DEPLOYMENTS = (
    "Follow-up: filter deployments/upgrade_runs/deployment_logs by the JWT "
    "tenant (handlers query by id only)."
)
_FU_NODE_LXD_JOIN = (
    "Follow-up: verify cluster_id belongs to the caller's tenant and SSRF-vet "
    "api_url before minting an LXD join token."
)
_FU_ELDER_STATUS = (
    "Follow-up: stop echoing exception text (str(e)) and make the outbound "
    "health probe connect to the vetted pinned IP."
)

INTENTIONALLY_UNMAPPED: dict[tuple[str, str], str] = {
    # Storage
    ("GET", "/api/v1/storage/configs"): _FU_STORAGE_TENANT,
    ("POST", "/api/v1/storage/configs"): _FU_STORAGE_TENANT,
    ("GET", "/api/v1/storage/configs/<int:config_id>"): _FU_STORAGE_TENANT,
    ("PUT", "/api/v1/storage/configs/<int:config_id>"): _FU_STORAGE_TENANT,
    ("DELETE", "/api/v1/storage/configs/<int:config_id>"): _FU_STORAGE_TENANT,
    ("POST", "/api/v1/storage/configs/<int:config_id>/set-default"): _FU_STORAGE_TENANT,
    ("POST", "/api/v1/storage/configs/<int:config_id>/test"): _FU_STORAGE_TENANT,
    ("GET", "/api/v1/storage/buckets"): _FU_STORAGE_TENANT,
    ("POST", "/api/v1/storage/buckets"): _FU_STORAGE_TENANT,
    ("GET", "/api/v1/storage/objects"): _FU_STORAGE_TENANT,
    ("DELETE", "/api/v1/storage/objects/<path:object_key>"): _FU_STORAGE_TENANT,
    ("POST", "/api/v1/storage/objects/<path:object_key>/presigned-url"): _FU_STORAGE_TENANT,
    ("GET", "/api/v1/storage/quotas"): _FU_STORAGE_QUOTA,
    ("POST", "/api/v1/storage/quota-request"): _FU_STORAGE_QUOTA,
    # Teams
    ("GET", "/api/v1/teams/"): _FU_TEAMS,
    ("POST", "/api/v1/teams/"): _FU_TEAMS,
    ("GET", "/api/v1/teams/<int:team_id>"): _FU_TEAMS,
    ("PATCH", "/api/v1/teams/<int:team_id>"): _FU_TEAMS,
    ("DELETE", "/api/v1/teams/<int:team_id>"): _FU_TEAMS,
    ("GET", "/api/v1/teams/<int:team_id>/members"): _FU_TEAMS,
    ("POST", "/api/v1/teams/<int:team_id>/members"): _FU_TEAMS,
    ("DELETE", "/api/v1/teams/<int:team_id>/members/<int:user_id>"): _FU_TEAMS,
    ("GET", "/api/v1/teams/<int:team_id>/resources"): _FU_TEAMS,
    ("POST", "/api/v1/teams/<int:team_id>/resources"): _FU_TEAMS,
    ("DELETE", "/api/v1/teams/<int:team_id>/resources/<int:assignment_id>"): _FU_TEAMS,
    # Clouds
    ("GET", "/api/v1/clouds/"): _FU_CLOUDS,
    ("POST", "/api/v1/clouds/"): _FU_CLOUDS,
    ("GET", "/api/v1/clouds/<int:provider_id>"): _FU_CLOUDS,
    ("PUT", "/api/v1/clouds/<int:provider_id>"): _FU_CLOUDS,
    ("DELETE", "/api/v1/clouds/<int:provider_id>"): _FU_CLOUDS,
    ("POST", "/api/v1/clouds/<int:provider_id>/test"): _FU_CLOUDS,
    ("GET", "/api/v1/clouds/<int:provider_id>/images"): _FU_CLOUDS,
    ("GET", "/api/v1/clouds/<int:provider_id>/regions"): _FU_CLOUDS,
    ("GET", "/api/v1/clouds/<int:provider_id>/sizes"): _FU_CLOUDS,
    ("GET", "/api/v1/clouds/<int:provider_id>/machines"): _FU_CLOUDS,
    ("POST", "/api/v1/clouds/<int:provider_id>/machines"): _FU_CLOUDS,
    ("GET", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>"): _FU_CLOUDS,
    ("DELETE", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>"): _FU_CLOUDS,
    ("POST", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>/start"): _FU_CLOUDS,
    ("POST", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>/stop"): _FU_CLOUDS,
    ("POST", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>/reboot"): _FU_CLOUDS,
    # iPXE reads (raw row echo)
    ("GET", "/api/v1/ipxe/config"): _FU_IPXE_READ,
    ("GET", "/api/v1/ipxe/boot-configs"): _FU_IPXE_READ,
    ("GET", "/api/v1/ipxe/boot-configs/<int:config_id>"): _FU_IPXE_READ,
    ("GET", "/api/v1/ipxe/boot-configs/<int:config_id>/preview"): _FU_IPXE_READ,
    ("GET", "/api/v1/ipxe/images"): _FU_IPXE_READ,
    ("GET", "/api/v1/ipxe/images/<int:image_id>"): _FU_IPXE_READ,
    ("GET", "/api/v1/ipxe/elder/status"): _FU_ELDER_STATUS,
    # iPXE machines
    ("GET", "/api/v1/ipxe/machines"): _FU_IPXE_MACHINES,
    ("GET", "/api/v1/ipxe/machines/<string:machine_id>"): _FU_IPXE_MACHINES,
    ("DELETE", "/api/v1/ipxe/machines/<string:machine_id>"): _FU_IPXE_MACHINES,
    ("PUT", "/api/v1/ipxe/machines/<string:machine_id>/biomes"): _FU_IPXE_MACHINES,
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/commission"): _FU_IPXE_MACHINES,
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/deploy"): _FU_IPXE_MACHINES,
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/release"): _FU_IPXE_MACHINES,
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/power/<string:action>"): _FU_IPXE_MACHINES,
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/sync-elder"): _FU_IPXE_MACHINES,
    # Agents
    ("GET", "/api/v1/agents/"): _FU_AGENTS,
    ("GET", "/api/v1/agents/<agent_id>"): _FU_AGENTS,
    ("POST", "/api/v1/agents/<agent_id>/suspend"): _FU_AGENTS,
    ("POST", "/api/v1/agents/<agent_id>/resume"): _FU_AGENTS,
    ("GET", "/api/v1/agents/enrollment-keys"): _FU_AGENTS,
    ("POST", "/api/v1/agents/enrollment-keys"): _FU_AGENTS,
    ("DELETE", "/api/v1/agents/enrollment-keys/<int:key_id>"): _FU_AGENTS,
    # Shell
    ("GET", "/api/v1/shell/sessions"): _FU_SHELL,
    ("POST", "/api/v1/shell/sessions"): _FU_SHELL,
    ("DELETE", "/api/v1/shell/sessions/<session_id>"): _FU_SHELL,
    # Biome deployments / upgrade runs
    ("GET", "/api/v1/biomes/deployments"): _FU_DEPLOYMENTS,
    ("GET", "/api/v1/biomes/deployments/<string:deployment_id>"): _FU_DEPLOYMENTS,
    ("GET", "/api/v1/biomes/deployments/<string:deployment_id>/logs"): _FU_DEPLOYMENTS,
    ("POST", "/api/v1/biomes/deployments/<string:deployment_id>/cancel"): _FU_DEPLOYMENTS,
    ("GET", "/api/v1/biomes/<int:biome_id>/upgrade-runs/<run_id>"): _FU_DEPLOYMENTS,
    # Nodes
    ("POST", "/api/v1/nodes/<int:node_id>/lxd/join"): _FU_NODE_LXD_JOIN,
}


# ==============================================================================
# Well-Formedness Validation (Load-Time Enforcement)
# ==============================================================================

def assert_policy_well_formed() -> None:
    """Validate scope policy at import time."""
    seen: set[tuple[str, str]] = set()
    for (method, path), scopes in SCOPE_POLICY.items():
        if (method, path) in seen:
            raise ValueError(f"Duplicate entry: ({method}, {path})")
        seen.add((method, path))
        if scopes is not None:
            unknown = scopes - KNOWN_SCOPES
            if unknown:
                raise ValueError(f"Unknown scopes at ({method}, {path})")

    policy_keys = set(SCOPE_POLICY.keys())
    for anon_path in ANONYMOUS_PATHS:
        if anon_path in policy_keys:
            raise ValueError(f"Anonymous path {anon_path} conflicts with policy")

    # An intentionally-unmapped route must stay unreachable: never granted a
    # scope or anonymous access, and always carrying its follow-up reason.
    for unmapped, reason in INTENTIONALLY_UNMAPPED.items():
        if unmapped in policy_keys or unmapped in ANONYMOUS_PATHS:
            raise ValueError(f"Intentionally-unmapped route {unmapped} is also granted")
        if not reason or "Follow-up:" not in reason:
            raise ValueError(f"Intentionally-unmapped route {unmapped} lacks a follow-up")


# Enforce well-formedness at import time
assert_policy_well_formed()


# ==============================================================================
# Role -> Scope Bundles (expanded at token issuance)
# ==============================================================================
#
# Roles are pre-bundled scope sets, per the security model -- authorization is
# always evaluated on scopes, never on role names. These bundles are expanded
# into the token's ``scope`` claim at login/refresh (see ``_expand_roles_to_scopes``).
# Bundles are derived from the real ``KNOWN_SCOPES`` catalog above so they can
# never drift out of it.

# Every read-only scope (``*.read``) -- the viewer bundle.
_READ_SCOPES: frozenset[str] = frozenset(s for s in KNOWN_SCOPES if s.endswith(".read"))

# Admin-only cluster control.
_ADMIN_SCOPES: frozenset[str] = frozenset({
    "gough.cluster.admin",
    "gough.cluster.superadmin",
})

# Dangerous override scopes (explicit approval / MFA class -- never in a role bundle).
_DANGEROUS_SCOPES: frozenset[str] = frozenset({
    "gough.dr.promote",
    "gough.biomes.unsafe-skip-signing",
    "gough.migration.override-lock",
})

# Destructive delete-class scopes excluded from maintainer.
_DELETE_SCOPES: frozenset[str] = frozenset({"gough.nodes.decommission"})

# Maintainer: read + write/author/provision/deploy, but no admin/superadmin,
# no dangerous overrides, and no destructive deletes.
_MAINTAINER_SCOPES: frozenset[str] = (
    KNOWN_SCOPES - _ADMIN_SCOPES - _DANGEROUS_SCOPES - _DELETE_SCOPES
)

ROLE_TO_SCOPE_BUNDLE: dict[str, frozenset[str]] = {
    # Dangerous overrides (dr.promote, unsafe-skip-signing, override-lock) are
    # never granted by a role bundle -- they require explicit approval/MFA and
    # must be minted deliberately, not implied by "admin".
    "admin": KNOWN_SCOPES - _DANGEROUS_SCOPES,
    "maintainer": _MAINTAINER_SCOPES,
    "viewer": _READ_SCOPES,
}


def _expand_roles_to_scopes(role_names: list[str]) -> list[str]:
    """Expand a user's role names into the union of their scope bundles.

    Unknown role names contribute nothing (fail-closed). Returns a sorted list
    suitable for the JWT ``scope`` claim.
    """
    scopes: set[str] = set()
    for role in role_names:
        scopes |= ROLE_TO_SCOPE_BUNDLE.get(role, frozenset())
    return sorted(scopes)


# ==============================================================================
# Anonymous-path matching (method + template aware)
# ==============================================================================
#
# ANONYMOUS_PATHS holds ``(method, flask-template-path)`` tuples, some of which
# are parameterised (iPXE netboot routes). These helpers match a concrete
# request path against them so both gough's ``before_request`` layers and the
# penguin-aaa ASGI auth middleware skip auth on the right routes.


def _anon_template_to_regex(policy_path: str) -> re.Pattern[str]:
    """Compile a Flask ``<type:name>`` template path into a concrete-path regex."""
    parts = re.split(r"<[^>]+>", policy_path)
    pattern = r"[^/]+".join(re.escape(p) for p in parts)
    return re.compile(r"^" + pattern + r"$")


_ANON_EXACT_PATHS: frozenset[str] = frozenset(
    path for (_method, path) in ANONYMOUS_PATHS if "<" not in path
)
_ANON_TEMPLATED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, _anon_template_to_regex(path))
    for (method, path) in ANONYMOUS_PATHS
    if "<" in path
)


def is_anonymous_path(path: str) -> bool:
    """Path-only (method-agnostic) anonymous test for the ASGI auth middleware."""
    if path in _ANON_EXACT_PATHS:
        return True
    return any(regex.match(path) for (_method, regex) in _ANON_TEMPLATED)


def is_anonymous_request(method: str, path: str) -> bool:
    """Method + template aware anonymous test for gough's before_request layers."""
    if (method, path) in ANONYMOUS_PATHS:
        return True
    for (anon_method, regex) in _ANON_TEMPLATED:
        if anon_method == method and regex.match(path):
            return True
    return False


class _AnonymousPathSet(set[str]):
    """``public_paths`` for penguin-aaa's ``OIDCAuthMiddleware``.

    The middleware tests membership as ``path in public_paths`` using the
    request path only (no method). A plain ``set[str]`` of literals cannot
    express gough's parameterised anonymous routes (e.g.
    ``/api/v1/ipxe/helper/<string:mac>``), so this ``set`` subclass overrides
    ``__contains__`` to be template-aware while still satisfying the
    middleware's ``set[str]`` type. It is intentionally left empty; only
    ``__contains__`` is consulted. Method-agnostic here is safe: skipping the
    bearer check on a wrong-method hit to an anonymous path still lands on
    Quart's own routing (405/404) -- it never exposes a protected handler.
    """

    def __contains__(self, path: object) -> bool:
        return isinstance(path, str) and is_anonymous_path(path)

    def __bool__(self) -> bool:
        # The set is intentionally empty (matching is done in __contains__), but
        # OIDCAuthMiddleware does ``public_paths or set()`` -- an empty set is
        # falsy and would be discarded, silently dropping every anonymous
        # bypass. Force truthiness so the middleware keeps this instance.
        return True


ANONYMOUS_PATH_SET: set[str] = _AnonymousPathSet()
