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
    ("GET", "/api/v1/nodes/<int:node_id>/capacity"): frozenset({"gough.nodes.read"}),
    ("PUT", "/api/v1/agents/<string:agent_id>/node"): frozenset({"gough.nodes.provision"}),
    ("PATCH", "/api/v1/nodes/<int:node_id>"): frozenset({"gough.nodes.provision"}),
    ("POST", "/api/v1/nodes/<int:node_id>/deploy"): frozenset({"gough.nodes.provision"}),
    ("POST", "/api/v1/nodes/<int:node_id>/reject"): frozenset({"gough.nodes.provision"}),
    ("POST", "/api/v1/nodes/<int:node_id>/rekey"): frozenset({"gough.nodes.rekey"}),
    ("POST", "/api/v1/nodes/<int:node_id>/evacuate"): frozenset({"gough.nodes.provision"}),
    ("DELETE", "/api/v1/nodes/<int:node_id>"): frozenset({"gough.nodes.decommission"}),
    # POST /nodes/<id>/events authenticates with a service SVID (mTLS) inside
    # the handler, not an OIDC bearer -> ANONYMOUS_PATHS (see below).
    ("GET", "/api/v1/nodes/<int:node_id>/tags"): frozenset({"gough.nodes.read"}),
    ("PATCH", "/api/v1/nodes/<int:node_id>/tags"): frozenset({"gough.nodes.provision"}),
    ("POST", "/api/v1/nodes/manual"): frozenset({"gough.nodes.provision", "gough.cluster.admin"}),
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
    ("POST", "/api/v1/lxd/node-join-keys"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/biomes/cache/imports"): frozenset({"gough.cluster.admin"}),

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

    # DR endpoints
    ("POST", "/api/v1/dr/drill"): frozenset({"gough.dr.drill"}),
    ("GET", "/api/v1/dr/drills"): frozenset({"gough.dr.read"}),
    ("POST", "/api/v1/dr/promote"): frozenset({"gough.dr.promote"}),

    # Webhooks endpoints
    ("GET", "/api/v1/webhooks"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/webhooks"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/webhooks/<int:webhook_id>"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/webhooks/<int:webhook_id>/test"): frozenset({"gough.cluster.admin"}),
    # GET /webhooks/keys/<tenant> serves a PUBLIC JWKS -> ANONYMOUS_PATHS (below).

    # Integrations endpoints
    ("GET", "/api/v1/integrations/status"): frozenset({"gough.cluster.read"}),
    ("POST", "/api/v1/integrations/<string:product>/configure"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/integrations/<string:product>/rotate-credentials"): frozenset({"gough.cluster.admin"}),

    # iPXE endpoints
    ("POST", "/api/v1/ipxe/bind-mac"): frozenset({"gough.nodes.provision"}),
    ("POST", "/api/v1/ipxe/mint-bootstrap-token"): frozenset({"gough.nodes.provision"}),

    # Webhooks
    ("GET", "/api/v1/webhooks"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/webhooks"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/webhooks/<string:webhook_id>"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/webhooks/<string:webhook_id>/test"): frozenset({"gough.cluster.admin"}),

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
    # Endpoints that previously had NO entry (and so were denied 403 by the fail-closed
    # middleware). Reads map to the matching *.read scope, writes to the matching
    # write/admin scope; secrets, users, shell and agent management are admin-only.
    ("DELETE", "/api/v1/agents/enrollment-keys/<int:key_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/clouds/<int:provider_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/ipxe/boot-configs/<int:config_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/ipxe/images/<int:image_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/ipxe/machines/<string:machine_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/joiner-secrets/clusters/<uuid:cluster_id>/joiner-secrets/<uuid:js_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/secrets/<path:secret_path>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/shell/sessions/<session_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/storage/configs/<int:config_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/storage/objects/<path:object_key>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/teams/<int:team_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/teams/<int:team_id>/members/<int:user_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/teams/<int:team_id>/resources/<int:assignment_id>"): frozenset({"gough.cluster.admin"}),
    ("DELETE", "/api/v1/users/<int:user_id>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/agents"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/agents/<agent_id>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/agents/enrollment-keys"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/biomes/<int:biome_id>/upgrade-runs/<run_id>"): frozenset({"gough.biomes.read"}),
    ("GET", "/api/v1/biomes/deployments"): frozenset({"gough.biomes.read"}),
    ("GET", "/api/v1/biomes/deployments/<string:deployment_id>"): frozenset({"gough.biomes.read"}),
    ("GET", "/api/v1/biomes/deployments/<string:deployment_id>/logs"): frozenset({"gough.biomes.read"}),
    ("GET", "/api/v1/clouds"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/clouds/<int:provider_id>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/clouds/<int:provider_id>/images"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/clouds/<int:provider_id>/machines"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/clouds/<int:provider_id>/regions"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/clouds/<int:provider_id>/sizes"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/clusters/<cluster_id>/lxd/status"): frozenset({"gough.cluster.read"}),
    ("GET", "/api/v1/clusters/<cluster_id>/network-baseline-topology"): frozenset({"gough.cluster.read"}),
    ("GET", "/api/v1/disks/<int:node_id>/disks"): frozenset({"gough.disks.read"}),
    ("GET", "/api/v1/hello"): frozenset(),
    ("GET", "/api/v1/hello/protected"): frozenset({"gough.nodes.provision"}),
    ("GET", "/api/v1/integrations/<product>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/ipxe/boot-configs"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/ipxe/boot-configs/<int:config_id>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/ipxe/boot-configs/<int:config_id>/preview"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/ipxe/config"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/ipxe/elder/status"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/ipxe/images"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/ipxe/images/<int:image_id>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/ipxe/machines"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/ipxe/machines/<string:machine_id>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/joiner-secrets/clusters/<uuid:cluster_id>/joiner-secrets"): frozenset({"gough.joiner.read"}),
    ("GET", "/api/v1/nodes/<int:node_id>/cloud-init"): frozenset({"gough.nodes.read"}),
    ("GET", "/api/v1/secrets"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/secrets/<path:secret_path>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/secrets/backends"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/shell/sessions"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/ssh-ca/public-key"): frozenset({"gough.cluster.read"}),
    ("GET", "/api/v1/storage/buckets"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/storage/configs"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/storage/configs/<int:config_id>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/storage/objects"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/storage/quotas"): frozenset({"gough.storage.read"}),
    ("GET", "/api/v1/teams"): frozenset({"gough.cluster.read"}),
    ("GET", "/api/v1/teams/<int:team_id>"): frozenset({"gough.cluster.read"}),
    ("GET", "/api/v1/teams/<int:team_id>/members"): frozenset({"gough.cluster.read"}),
    ("GET", "/api/v1/teams/<int:team_id>/resources"): frozenset({"gough.cluster.read"}),
    ("GET", "/api/v1/users"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/users/<int:user_id>"): frozenset({"gough.cluster.admin"}),
    ("GET", "/api/v1/users/roles"): frozenset({"gough.cluster.admin"}),
    ("PATCH", "/api/v1/clusters/<cluster_id>/network-baseline-topology"): frozenset({"gough.cluster.admin"}),
    ("PATCH", "/api/v1/disks/<int:node_id>/disks/<int:disk_id>"): frozenset({"gough.disks.plan"}),
    ("PATCH", "/api/v1/teams/<int:team_id>"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/agents/<agent_id>/resume"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/agents/<agent_id>/suspend"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/agents/enrollment-keys"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/biomes/deployments/<string:deployment_id>/cancel"): frozenset({"gough.biomes.deploy"}),
    ("POST", "/api/v1/clouds"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/clouds/<int:provider_id>/machines"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>/reboot"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>/start"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>/stop"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/clouds/<int:provider_id>/test"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/disks/<int:node_id>/disks/<int:disk_id>/plan"): frozenset({"gough.disks.plan"}),
    ("POST", "/api/v1/disks/<int:node_id>/disks/<int:disk_id>/smart-recheck"): frozenset({"gough.disks.plan"}),
    ("POST", "/api/v1/integrations/<product>/compromise-response"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/integrations/<product>/validate-scope"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/ipxe/boot-configs"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/ipxe/images"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/commission"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/deploy"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/power/<string:action>"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/release"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/sync-elder"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/joiner-secrets/clusters/<uuid:cluster_id>/joiner-secrets/<uuid:js_id>/rotate"): frozenset({"gough.joiner.rotate"}),
    ("POST", "/api/v1/nodes/<int:node_id>/lxd/join"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/primary/frontend-switch"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/primary/rotate-ca"): frozenset({"gough.cluster.superadmin"}),
    ("POST", "/api/v1/secrets/<path:secret_path>"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/secrets/backends"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/shell/sessions"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/ssh-ca/initialize"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/ssh-ca/sign"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/storage/buckets"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/storage/configs"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/storage/configs/<int:config_id>/set-default"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/storage/configs/<int:config_id>/test"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/storage/objects/<path:object_key>/presigned-url"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/storage/quota-request"): frozenset({"gough.storage.configure"}),
    ("POST", "/api/v1/teams"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/teams/<int:team_id>/members"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/teams/<int:team_id>/resources"): frozenset({"gough.cluster.admin"}),
    ("POST", "/api/v1/users"): frozenset({"gough.cluster.admin"}),
    ("PUT", "/api/v1/clouds/<int:provider_id>"): frozenset({"gough.cluster.admin"}),
    ("PUT", "/api/v1/ipxe/boot-configs/<int:config_id>"): frozenset({"gough.cluster.admin"}),
    ("PUT", "/api/v1/ipxe/config"): frozenset({"gough.cluster.admin"}),
    ("PUT", "/api/v1/ipxe/elder/config"): frozenset({"gough.cluster.admin"}),
    ("PUT", "/api/v1/ipxe/images/<int:image_id>"): frozenset({"gough.cluster.admin"}),
    ("PUT", "/api/v1/ipxe/machines/<string:machine_id>/biomes"): frozenset({"gough.cluster.admin"}),
    ("PUT", "/api/v1/secrets/<path:secret_path>"): frozenset({"gough.cluster.admin"}),
    ("PUT", "/api/v1/storage/configs/<int:config_id>"): frozenset({"gough.cluster.admin"}),
    ("PUT", "/api/v1/users/<int:user_id>"): frozenset({"gough.cluster.admin"}),
}


# Routes whose handlers were NEVER reachable (the fail-closed middleware 403'd them) until
# 385dd0f gave them a scope, so they were never security-reviewed. They operate on
# deployment-global tables with no tenant column (users, teams, cloud providers, storage
# configs, iPXE inventory, shell/SSH-CA) or on a shared CA, so a tenant-scoped admin can touch
# other tenants' data until a platform-scope design (system tenant / per-row tenant_id) lands.
# Every entry is held at ``gough.cluster.admin`` as the interim containment. This list must
# only SHRINK: tests/security/test_scope_policy_route_coverage.py fails if it grows or if an
# entry drops below cluster.admin. Remove an entry only after the handler is tenant-scoped and
# reviewed (see services/api-manager route audit).
UNREVIEWED_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("DELETE", "/api/v1/clouds/<int:provider_id>"),
    ("DELETE", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>"),
    ("DELETE", "/api/v1/ipxe/boot-configs/<int:config_id>"),
    ("DELETE", "/api/v1/ipxe/images/<int:image_id>"),
    ("DELETE", "/api/v1/ipxe/machines/<string:machine_id>"),
    ("DELETE", "/api/v1/storage/configs/<int:config_id>"),
    ("DELETE", "/api/v1/storage/objects/<path:object_key>"),
    ("DELETE", "/api/v1/teams/<int:team_id>"),
    ("DELETE", "/api/v1/teams/<int:team_id>/members/<int:user_id>"),
    ("DELETE", "/api/v1/teams/<int:team_id>/resources/<int:assignment_id>"),
    ("DELETE", "/api/v1/users/<int:user_id>"),
    ("GET", "/api/v1/clouds"),
    ("GET", "/api/v1/clouds/<int:provider_id>"),
    ("GET", "/api/v1/clouds/<int:provider_id>/images"),
    ("GET", "/api/v1/clouds/<int:provider_id>/machines"),
    ("GET", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>"),
    ("GET", "/api/v1/clouds/<int:provider_id>/regions"),
    ("GET", "/api/v1/clouds/<int:provider_id>/sizes"),
    ("GET", "/api/v1/integrations/<product>"),
    ("GET", "/api/v1/ipxe/boot-configs"),
    ("GET", "/api/v1/ipxe/boot-configs/<int:config_id>"),
    ("GET", "/api/v1/ipxe/boot-configs/<int:config_id>/preview"),
    ("GET", "/api/v1/ipxe/config"),
    ("GET", "/api/v1/ipxe/elder/status"),
    ("GET", "/api/v1/ipxe/images"),
    ("GET", "/api/v1/ipxe/images/<int:image_id>"),
    ("GET", "/api/v1/ipxe/machines"),
    ("GET", "/api/v1/ipxe/machines/<string:machine_id>"),
    ("GET", "/api/v1/storage/buckets"),
    ("GET", "/api/v1/storage/configs"),
    ("GET", "/api/v1/storage/configs/<int:config_id>"),
    ("GET", "/api/v1/storage/objects"),
    ("GET", "/api/v1/users"),
    ("GET", "/api/v1/users/<int:user_id>"),
    ("GET", "/api/v1/users/roles"),
    ("PATCH", "/api/v1/teams/<int:team_id>"),
    ("POST", "/api/v1/clouds"),
    ("POST", "/api/v1/clouds/<int:provider_id>/machines"),
    ("POST", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>/reboot"),
    ("POST", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>/start"),
    ("POST", "/api/v1/clouds/<int:provider_id>/machines/<machine_id>/stop"),
    ("POST", "/api/v1/clouds/<int:provider_id>/test"),
    ("POST", "/api/v1/integrations/<product>/validate-scope"),
    ("POST", "/api/v1/ipxe/boot-configs"),
    ("POST", "/api/v1/ipxe/images"),
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/commission"),
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/deploy"),
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/power/<string:action>"),
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/release"),
    ("POST", "/api/v1/ipxe/machines/<string:machine_id>/sync-elder"),
    ("POST", "/api/v1/shell/sessions"),
    ("POST", "/api/v1/ssh-ca/initialize"),
    ("POST", "/api/v1/ssh-ca/sign"),
    ("POST", "/api/v1/storage/buckets"),
    ("POST", "/api/v1/storage/configs"),
    ("POST", "/api/v1/storage/configs/<int:config_id>/set-default"),
    ("POST", "/api/v1/storage/configs/<int:config_id>/test"),
    ("POST", "/api/v1/storage/objects/<path:object_key>/presigned-url"),
    ("POST", "/api/v1/teams"),
    ("POST", "/api/v1/teams/<int:team_id>/members"),
    ("POST", "/api/v1/teams/<int:team_id>/resources"),
    ("POST", "/api/v1/users"),
    ("PUT", "/api/v1/clouds/<int:provider_id>"),
    ("PUT", "/api/v1/ipxe/boot-configs/<int:config_id>"),
    ("PUT", "/api/v1/ipxe/config"),
    ("PUT", "/api/v1/ipxe/elder/config"),
    ("PUT", "/api/v1/ipxe/images/<int:image_id>"),
    ("PUT", "/api/v1/ipxe/machines/<string:machine_id>/biomes"),
    ("PUT", "/api/v1/storage/configs/<int:config_id>"),
    ("PUT", "/api/v1/users/<int:user_id>"),
})


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
    ("GET", "/api/v1/ipxe/kernel/<string:name>"),
    ("GET", "/api/v1/ipxe/initrd/<string:name>"),
    ("GET", "/api/v1/ipxe/helper-efi/<string:mac>"),
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
    # LXD cluster join: HS256 agent_access bearer validated in the handler
    # (app.api.lxd_cluster._authenticate_agent); agent must hold a join-eligible key.
    ("POST", "/api/v1/lxd/cluster-join-tokens"),
    # Biome bundle file read: HS256 agent_access bearer validated in the handler
    # (app.api.biome_cache); agent must be active; path traversal rejected in-handler.
    ("GET", "/api/v1/biomes/cache/<string:version>/<string:biome>/<path:path>"),
    # Installer distribution: the script and its sha256 are public by design
    # (rate-limited in app.api.install). Nothing else under /install is anonymous.
    ("GET", "/api/v1/install/gough-install.py"),
    ("GET", "/api/v1/install/gough-install.py.sha256"),
})


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
    """Compile a Flask ``<type:name>`` template path into a concrete-path regex.

    ``<path:...>`` converters span slashes (nested biome file paths); every other
    converter matches a single segment. Handlers validate the captured value.
    """
    out: list[str] = []
    for token in re.split(r"(<[^>]+>)", policy_path):
        if token.startswith("<") and token.endswith(">"):
            out.append(r".+" if token.startswith("<path:") else r"[^/]+")
        else:
            out.append(re.escape(token))
    return re.compile(r"^" + "".join(out) + r"$")


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
