"""OpenTelemetry bootstrap for gough's M2 cluster-execution paths.

Scoped deliberately, not app-wide: no other module in api-manager
instruments OTel yet (``app/metrics.py`` is Prometheus-only, and logging is
bare stdlib ``logging`` throughout -- a pre-existing gap outside this
change's scope). Bootstrapping a ``TracerProvider``/``MeterProvider``/
``LoggerProvider`` here keeps the blast radius of a new, non-trivial
dependency (``opentelemetry-sdk`` + the OTLP gRPC exporters) to exactly the
two M2 routes that need logs + metrics + traces (``app/api/clusters.py``'s
``switch_primary_storage`` and ``adopt_cluster`` execution paths).

Destination is always env-configurable, never hardcoded, per
``critical-rules.md`` Observability (OTel):

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` -- collector URL. Unset => every provider
  below runs with no exporter attached, so span/metric/log calls are cheap
  no-ops rather than failures ("a dead exporter never breaks the app").
* ``OTEL_EXPORTER_OTLP_HEADERS`` -- ``k1=v1,k2=v2`` auth headers, from
  env/secret only.
* ``OTEL_SERVICE_NAME`` -- defaults to ``gough-api-manager``.
* ``OTEL_RESOURCE_ATTRIBUTES`` -- ``k1=v1,k2=v2`` extra resource attributes.

Exporter wiring is wrapped so a misconfigured/unreachable collector can
never crash startup or a request -- see ``_ensure_initialized``.
"""

from __future__ import annotations

import logging
import os
import threading

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Meter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer

_bootstrap_log = logging.getLogger(__name__)

_lock = threading.Lock()
_initialized = False
_log_handler: LoggingHandler | None = None


def _parse_kv_csv(raw: str) -> dict[str, str]:
    """Parse ``k1=v1,k2=v2`` strings used by the OTEL_* env vars."""
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def _build_resource() -> Resource:
    attrs: dict[str, str] = {
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "gough-api-manager"),
    }
    attrs.update(_parse_kv_csv(os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")))
    return Resource.create(attrs)


def _ensure_initialized() -> None:
    """Idempotently install the global Tracer/Meter/Logger providers.

    Safe to call repeatedly (including across module reloads in tests) --
    only the first call does real work.
    """
    global _initialized, _log_handler
    if _initialized:
        return
    with _lock:
        if _initialized:
            return

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        headers = _parse_kv_csv(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")) or None
        resource = _build_resource()

        tracer_provider = TracerProvider(resource=resource)
        logger_provider = LoggerProvider(resource=resource)
        metric_readers: list[PeriodicExportingMetricReader] = []

        if endpoint:
            try:
                tracer_provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers))
                )
                metric_readers.append(
                    PeriodicExportingMetricReader(
                        OTLPMetricExporter(endpoint=endpoint, headers=headers)
                    )
                )
                logger_provider.add_log_record_processor(
                    BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, headers=headers))
                )
            except Exception:  # noqa: BLE001 - exporter wiring must never crash startup
                _bootstrap_log.warning(
                    "OTel OTLP exporter setup failed for endpoint=%s; continuing "
                    "with no-op telemetry (logs/metrics/traces still emitted "
                    "locally via stdlib logging + Prometheus, see app/metrics.py)",
                    endpoint,
                    exc_info=True,
                )

        meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)

        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        _log_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
        _initialized = True


def get_tracer(name: str) -> Tracer:
    """Return a Tracer bound to ``name`` (typically ``__name__``)."""
    _ensure_initialized()
    return trace.get_tracer(name)


def get_meter(name: str) -> Meter:
    """Return a Meter bound to ``name`` (typically ``__name__``)."""
    _ensure_initialized()
    return metrics.get_meter(name)


def attach_otel_logging(logger: logging.Logger) -> None:
    """Attach the shared OTel ``LoggingHandler`` to ``logger`` (idempotent).

    Every ``logger.info``/``.warning``/``.error`` call on ``logger`` is then
    also emitted as an OTel log record (exported when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is configured) in addition to its normal
    stdlib handler output -- neither substitutes for the other, per
    ``critical-rules.md`` Observability (OTel).
    """
    _ensure_initialized()
    if _log_handler is not None and _log_handler not in logger.handlers:
        logger.addHandler(_log_handler)


__all__ = ["attach_otel_logging", "get_meter", "get_tracer"]
