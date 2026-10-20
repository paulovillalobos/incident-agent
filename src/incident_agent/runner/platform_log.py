"""Platform events (rollouts, restarts) exported as OTLP log records, like cluster events."""

from __future__ import annotations

import time

import httpx
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.resource.v1.resource_pb2 import Resource

_SEVERITY = {"INFO": 9, "WARN": 13, "ERROR": 17}


class PlatformLog:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/") + "/v1/logs"
        self.client = httpx.Client(timeout=5)

    def emit(self, service: str, message: str, severity: str = "INFO") -> None:
        record = LogRecord(
            time_unix_nano=time.time_ns(),
            severity_text=severity,
            severity_number=_SEVERITY.get(severity, 9),  # type: ignore[arg-type]
            body=AnyValue(string_value=message),
        )
        resource = Resource(
            attributes=[KeyValue(key="service.name", value=AnyValue(string_value=service))]
        )
        req = ExportLogsServiceRequest(
            resource_logs=[
                ResourceLogs(resource=resource, scope_logs=[ScopeLogs(log_records=[record])])
            ]
        )
        self.client.post(
            self.endpoint,
            content=req.SerializeToString(),
            headers={"Content-Type": "application/x-protobuf"},
        )
