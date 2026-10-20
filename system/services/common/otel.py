"""OpenTelemetry wiring shared by every service: traces, metrics and logs over OTLP/HTTP.

Every log record emitted through the standard ``logging`` module is exported with the
trace id and span id of the active span, so logs can be joined to traces.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Iterable

import psutil
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import AggregationTemporality, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from services.common.container import peak_rss_bytes

DEFAULT_ENDPOINT = "http://127.0.0.1:47318"

_providers: list[TracerProvider | MeterProvider | LoggerProvider] = []


class _TraceContextFormatter(logging.Formatter):
    """Console formatter that prints the active trace/span ids next to each line."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        record.trace_ids = (
            f"trace={ctx.trace_id:032x} span={ctx.span_id:016x}" if ctx.is_valid else "trace=-"
        )
        return super().format(record)


def setup(service: str, version: str) -> Callable[[], None]:
    """Install tracer, meter and logger providers for ``service``; return a flush function."""
    endpoint = os.environ.get("IA_OTLP_ENDPOINT", DEFAULT_ENDPOINT)
    instance = os.environ.get("IA_INSTANCE", service)
    resource = Resource.create(
        {
            "service.name": service,
            "service.version": version,
            "service.instance.id": instance,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"),
            schedule_delay_millis=500,
            max_queue_size=16384,
        )
    )
    trace.set_tracer_provider(tracer_provider)

    delta = AggregationTemporality.DELTA
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(
            endpoint=f"{endpoint}/v1/metrics",
            preferred_temporality={
                Counter: delta,
                Histogram: delta,
                ObservableCounter: delta,
                UpDownCounter: AggregationTemporality.CUMULATIVE,
                ObservableUpDownCounter: AggregationTemporality.CUMULATIVE,
            },
        ),
        export_interval_millis=2000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"), schedule_delay_millis=500
        )
    )
    set_logger_provider(logger_provider)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        _TraceContextFormatter("%(asctime)s %(levelname)s %(name)s %(trace_ids)s %(message)s")
    )
    root.addHandler(console)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # uvicorn repeats every exception the app handler already logged; keep one copy.
    logging.getLogger("uvicorn.error").addFilter(
        lambda record: not record.getMessage().startswith("Exception in ASGI application")
    )

    HTTPXClientInstrumentor().instrument()
    _register_process_metrics(service)
    _providers.extend([tracer_provider, meter_provider, logger_provider])
    return shutdown


def _register_process_metrics(service: str) -> None:
    process = psutil.Process()
    meter = metrics.get_meter(service)

    def rss(_: CallbackOptions) -> Iterable[Observation]:
        yield Observation(process.memory_info().rss)

    def peak(_: CallbackOptions) -> Iterable[Observation]:
        yield Observation(peak_rss_bytes())

    meter.create_observable_gauge(
        "process.memory.rss", callbacks=[rss], unit="By", description="Resident set size"
    )
    meter.create_observable_gauge(
        "process.memory.peak", callbacks=[peak], unit="By", description="Peak resident set size"
    )


def shutdown() -> None:
    """Flush and stop all providers so nothing buffered is lost on exit."""
    while _providers:
        _providers.pop().shutdown()


def tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def meter(name: str) -> metrics.Meter:
    return metrics.get_meter(name)
