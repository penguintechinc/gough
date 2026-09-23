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
