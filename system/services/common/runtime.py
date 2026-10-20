"""Process runtime helpers: configuration, ports, data directories and the HTTP server."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import tomllib
import traceback
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from services.common import otel


def system_root() -> Path:
    """Root of the system tree (``config/``, ``migrations/``, ``services/``)."""
    return Path(os.environ.get("IA_SYSTEM_ROOT", Path(__file__).resolve().parents[2]))


def load_config(service: str) -> dict[str, Any]:
    with (system_root() / "config" / f"{service}.toml").open("rb") as fh:
        return tomllib.load(fh)


def ports() -> dict[str, int]:
    raw = os.environ.get("IA_PORTS", "{}")
    parsed: dict[str, int] = json.loads(raw)
    return parsed


def url(name: str) -> str:
    return f"http://127.0.0.1:{ports()[name]}"


def data_dir() -> Path:
    path = Path(os.environ.get("IA_DATA_DIR", system_root() / ".data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_app(service: str) -> FastAPI:
    """FastAPI app with OTel server spans and a handler that logs unhandled exceptions."""
    app = FastAPI(title=service)
    log = logging.getLogger(service)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error(
            "unhandled %s: %s\n%s",
            type(exc).__name__,
            exc,
            "".join(traceback.format_exception(exc)).rstrip(),
        )
        return JSONResponse(status_code=500, content={"error": type(exc).__name__})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])
    return app


def serve(app: FastAPI, name: str) -> None:
    """Run uvicorn until SIGTERM, then flush telemetry.

    The listening socket uses SO_REUSEPORT so a new version can bind the same port before
    the old process drains (a rolling restart). Readiness is signalled by touching
    ``IA_READY_FILE`` once the server accepts connections.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("127.0.0.1", ports()[name]))
    config = uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    ready_file = os.environ.get("IA_READY_FILE")

    def signal_ready() -> None:
        while not server.started and not server.should_exit:
            time.sleep(0.02)
        if ready_file and server.started:
            open(ready_file, "w").close()

    threading.Thread(target=signal_ready, daemon=True).start()
    try:
        server.run(sockets=[sock])
    finally:
        otel.shutdown()
