"""A lean OTLP/HTTP receiver that writes spans, logs and metric points into SQLite.

It accepts ``POST /v1/traces``, ``/v1/metrics`` and ``/v1/logs`` with protobuf or JSON
bodies (optionally gzip-compressed), which is what the OpenTelemetry SDK exporters send.
It replaces the OpenTelemetry Collector for this project: the only pipeline needed is
"receive OTLP, store queryably", and a single dependency-free process is easier to run
on a laptop and in CI.

Run: ``python -m incident_agent.telemetry.receiver --db telemetry.db --port 47318``.
"""

from __future__ import annotations

import argparse
import gzip
import json
import queue
import threading
import time
from collections.abc import Iterable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import Message
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from incident_agent.telemetry import store

Row = tuple[Any, ...]
_KINDS = ["UNSPECIFIED", "INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER"]
_STATUS = {0: "unset", 1: "ok", 2: "error"}


def any_value(v: AnyValue) -> Any:
    which = v.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [any_value(x) for x in v.array_value.values]
    if which == "kvlist_value":
        return attributes(v.kvlist_value.values)
    if which == "bytes_value":
        return v.bytes_value.hex()
    return getattr(v, which)


def attributes(kvs: Iterable[KeyValue]) -> dict[str, Any]:
    return {kv.key: any_value(kv.value) for kv in kvs}


def _resource(attrs: dict[str, Any]) -> tuple[str, str | None, str | None]:
    service = str(attrs.get("service.name", "unknown"))
    return service, attrs.get("service.instance.id"), attrs.get("service.version")


def span_rows(req: ExportTraceServiceRequest) -> list[Row]:
    rows: list[Row] = []
    for rs in req.resource_spans:
        service, instance, version = _resource(attributes(rs.resource.attributes))
        for ss in rs.scope_spans:
            for s in ss.spans:
                attrs = attributes(s.attributes)
                http_status = attrs.get("http.response.status_code", attrs.get("http.status_code"))
                events = [
                    {
                        "name": e.name,
                        "ts_ns": e.time_unix_nano,
                        "attributes": attributes(e.attributes),
                    }
                    for e in s.events
                ]
                rows.append(
                    (
                        s.span_id.hex(),
                        s.trace_id.hex(),
                        s.parent_span_id.hex() or None,
                        service,
                        instance,
                        version,
                        s.name,
                        _KINDS[s.kind] if s.kind < len(_KINDS) else str(s.kind),
                        s.start_time_unix_nano,
                        s.end_time_unix_nano,
                        round((s.end_time_unix_nano - s.start_time_unix_nano) / 1e6, 3),
                        _STATUS.get(s.status.code, "unset"),
                        s.status.message or None,
                        int(http_status) if http_status is not None else None,
                        json.dumps(attrs),
                        json.dumps(events),
                    )
                )
    return rows


def log_rows(req: ExportLogsServiceRequest) -> list[Row]:
    rows: list[Row] = []
    for rl in req.resource_logs:
        service, instance, version = _resource(attributes(rl.resource.attributes))
        for sl in rl.scope_logs:
            for r in sl.log_records:
                body = any_value(r.body)
                rows.append(
                    (
                        r.time_unix_nano or r.observed_time_unix_nano,
                        service,
                        instance,
                        version,
                        r.severity_text or str(r.severity_number),
                        body if isinstance(body, str) else json.dumps(body),
                        r.trace_id.hex() or None,
                        r.span_id.hex() or None,
                        json.dumps(attributes(r.attributes)),
                    )
                )
    return rows


def metric_rows(req: ExportMetricsServiceRequest) -> list[Row]:
    rows: list[Row] = []
    for rm in req.resource_metrics:
        service, instance, _ = _resource(attributes(rm.resource.attributes))
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                which = m.WhichOneof("data")
                if which in ("gauge", "sum"):
                    for p in getattr(m, which).data_points:
                        value = p.as_double if p.WhichOneof("value") == "as_double" else p.as_int
                        rows.append(
                            (
                                p.time_unix_nano,
                                service,
                                instance,
                                m.name,
                                which,
                                float(value),
                                None,
                                None,
                                json.dumps(attributes(p.attributes)),
                            )
                        )
                elif which == "histogram":
                    for h in m.histogram.data_points:
                        mean = h.sum / h.count if h.count else None
                        rows.append(
                            (
                                h.time_unix_nano,
                                service,
                                instance,
                                m.name,
                                "histogram",
                                mean,
                                int(h.count),
                                float(h.sum),
                                json.dumps(attributes(h.attributes)),
                            )
                        )
    return rows


class Receiver:
    """Parses OTLP requests on HTTP threads and writes rows from a single writer thread."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.pending: queue.Queue[tuple[str, Sequence[Row]] | None] = queue.Queue()
        self.counts = {"spans": 0, "logs": 0, "metric_points": 0}
        self.last_request = time.monotonic()
        self._writer = threading.Thread(target=self._write_loop, name="sqlite-writer", daemon=True)
        self._writer.start()

    def _write_loop(self) -> None:
        conn = store.open_writable(self.db_path)
        sql = {
            "spans": "INSERT OR IGNORE INTO spans VALUES (" + ",".join("?" * 16) + ")",
            "logs": "INSERT INTO logs (ts_ns, service, instance, version, severity, body,"
            " trace_id, span_id, attributes) VALUES (?,?,?,?,?,?,?,?,?)",
            "metric_points": "INSERT INTO metric_points (ts_ns, service, instance, name, kind,"
            " value, count, sum, attributes) VALUES (?,?,?,?,?,?,?,?,?)",
        }
        while True:
            item = self.pending.get()
            if item is None:
                break
            table, rows = item
            with conn:
                conn.executemany(sql[table], rows)
            self.counts[table] += len(rows)
            self.pending.task_done()
        conn.close()

    def ingest(self, path: str, body: bytes, content_type: str) -> None:
        self.last_request = time.monotonic()
        handlers: dict[str, tuple[type[Message], str, Any]] = {
            "/v1/traces": (ExportTraceServiceRequest, "spans", span_rows),
            "/v1/logs": (ExportLogsServiceRequest, "logs", log_rows),
            "/v1/metrics": (ExportMetricsServiceRequest, "metric_points", metric_rows),
        }
        message_type, table, to_rows = handlers[path]
        message = message_type()
        if "json" in content_type:
            json_format.Parse(body, message)
        else:
            message.ParseFromString(body)
        rows = to_rows(message)
        if rows:
            self.pending.put((table, rows))

    def status(self) -> dict[str, Any]:
        return {
            "queued": self.pending.qsize(),
            "idle_s": round(time.monotonic() - self.last_request, 2),
            **self.counts,
        }

    def close(self) -> None:
        self.pending.join()
        self.pending.put(None)
        self._writer.join()


def make_server(receiver: Receiver, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_POST(self) -> None:
            if self.path not in ("/v1/traces", "/v1/logs", "/v1/metrics"):
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            try:
                receiver.ingest(self.path, body, self.headers.get("Content-Type", ""))
            except Exception as exc:
                self.send_error(400, str(exc)[:200])
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            if self.path != "/status":
                self.send_error(404)
                return
            payload = json.dumps(receiver.status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--port", type=int, default=47318)
    args = parser.parse_args(argv)
    receiver = Receiver(args.db)
    server = make_server(receiver, args.port)
    import signal

    def stop(*_: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        receiver.close()


if __name__ == "__main__":
    main()
