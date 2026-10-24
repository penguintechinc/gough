from typing import Any

from prometheus_client import Counter, Gauge, Histogram


def get_or_create(factory: Any, name: str, doc: str, labels: Any = None) -> Any:
    """Return the already-registered collector for ``name``, or create it.

    prometheus_client raises ``ValueError`` on duplicate registration, and a
    metric name can legitimately be declared by two modules that are imported
    into the same process (``app.api.audit`` and
    ``app.workers.audit_chain_writer`` both declare
    ``gough_audit_chain_break_total``). Whichever imports second would otherwise
    blow up at import time -- which surfaced as blueprint-import failures once
    both modules ended up in one test session.
    """
    try:
        if labels is not None:
            return factory(name, doc, labels)
        return factory(name, doc)
    except ValueError:
        from prometheus_client import REGISTRY

        for collector in REGISTRY._collector_to_names:
            if name in REGISTRY._collector_to_names.get(collector, ()):
                return collector
        raise


bootstrap_window_expired: Counter = Counter(
    "gough_vault_bootstrap_window_expired_total",
    "Bootstrap tokens that expired before the node used them.",
)

joiner_secret_decryption_failure: Counter = Counter(
    "gough_joiner_secrets_decryption_failure_total",
    "Joiner secret decryption failures during node join.",
)

biome_signature_verification_failed: Counter = Counter(
    "gough_biomes_signature_verification_failed_total",
    "Biome image signature verification failures.",
    ["environment"],
)

cluster_quorum_loss: Counter = Counter(
    "gough_cluster_quorum_loss_total",
    "k8s control plane quorum loss events during biome deployment.",
)

# --- Plan 4: Security & Identity ---

audit_chain_integrity_failure: Counter = Counter(
    "gough_audit_chain_integrity_failure_total",
    "Hash-chain breaks detected during audit chain verification",
    ["cluster_id"],
)

spiffe_svid_rotation_grace_exceeded: Counter = Counter(
    "gough_spiffe_svid_rotation_grace_exceeded_total",
    "SVID rotation deadline missed; certificate expiry imminent",
    ["service_name"],
)

security_otp_replay_detected: Counter = Counter(
    "gough_security_otp_replay_detected_total",
    "Joiner OTP secret used more than once (replay attack or bug)",
    ["node_id"],
)

audit_offsite_mirror_lag_seconds: Gauge = Gauge(
    "gough_audit_offsite_mirror_lag_seconds",
    "Seconds the offsite audit mirror lags behind the primary",
    ["cluster_id"],
)

grpc_auth_rejected_total: Counter = Counter(
    "gough_grpc_auth_rejected_total",
    "gRPC RPCs rejected by GrpcAuthInterceptor for missing/invalid SVID or machine JWT",
    ["method", "reason"],
)

# --- Plan 5: API Services & Management Interfaces ---

api_request_latency_seconds: Histogram = Histogram(
    "gough_api_request_latency_seconds",
    "API endpoint request latency in seconds",
    ["method", "endpoint", "status_code"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

api_error_total: Counter = Counter(
    "gough_api_error_total",
    "API error responses by endpoint and status code",
    ["method", "endpoint", "status_code"],
)

provisioning_queue_depth: Gauge = Gauge(
    "gough_provisioning_queue_depth",
    "Number of pending node provisioning tasks in the queue",
    ["tenant_id"],
)

deployment_queue_depth: Gauge = Gauge(
    "gough_deployment_queue_depth",
    "Number of pending biome deployments in the queue",
    ["tenant_id"],
)

nats_audit_mirror_lag_messages: Gauge = Gauge(
    "gough_nats_audit_mirror_lag_messages",
    "Number of audit events not yet mirrored to the offsite NATS stream",
    ["cluster_id"],
)

# --- M3: Anycast/BGP control-plane frontend ---

anycast_bgp_announce_failure: Counter = Counter(
    "gough_anycast_bgp_announce_failure_total",
    "BGP route advertise/withdraw failures on a primary node during an "
    "anycast control-plane frontend switch",
    ["node_id", "action"],
)

# --- Phase B: plan-compiler wiring (node deploy path) ---

plan_compile_total: Counter = Counter(
    "gough_plan_compile_total",
    "Plan-compiler invocations from the node deploy path, by outcome.",
    ["result"],
)

plan_compile_duration_seconds: Histogram = Histogram(
    "gough_plan_compile_duration_seconds",
    "Wall-clock time for PlanCompiler.compile() invoked from the deploy path.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# --- SPIRE join-token issuance (node discover path) ---

spire_join_token_issued_total: Counter = Counter(
    "gough_spire_join_token_issued_total",
    "SPIRE join-token issuance attempts during node discovery, by outcome.",
    ["result"],
)

# --- Cloud-KMS LUKS sealing (plan compiler, cloud-VM nodes) ---

cloud_kms_luks_seal_total: Counter = Counter(
    "gough_cloud_kms_luks_seal_total",
    "Cloud-KMS LUKS key sealing attempts for cloud-VM nodes, by provider and outcome.",
    ["provider", "result"],
)

# --- Resilience & telemetry integrity (degraded-mode visibility) ---

rate_limit_degraded: Gauge = Gauge(
    "gough_rate_limit_degraded",
    "1 when the rate limiter runs on in-memory storage because Redis was unreachable "
    "and RATE_LIMIT_ALLOW_MEMORY_FALLBACK was set; 0 otherwise.",
)

capacity_predictor_degraded: Gauge = Gauge(
    "gough_capacity_predictor_degraded",
    "1 when the capacity predictor or an optional dependency (WaddleAI, Prometheus, "
    "Redis cache) is unavailable; 0 when fully initialized.",
)

audit_logger_degraded: Gauge = Gauge(
    "gough_audit_logger_degraded",
    "1 when the audit logger failed to initialize at startup (audit persistence disabled); 0 otherwise.",
)

biome_seed_degraded: Gauge = Gauge(
    "gough_biome_seed_degraded",
    "1 when seeding built-in biomes failed at startup (runtime DB error); 0 otherwise.",
)

audit_nats_publish_failures_total: Counter = Counter(
    "gough_audit_nats_publish_failures_total",
    "Audit entry-written events that failed to publish to NATS.",
)

metrics_record_errors_total: Counter = Counter(
    "gough_metrics_record_errors_total",
    "Errors raised while recording per-request API metrics.",
)
