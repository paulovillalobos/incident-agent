"""gateway: public checkout API. Validates the request and forwards it to orders."""

from __future__ import annotations

import logging
import re

import httpx
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.common import http, otel, runtime

SERVICE = "gateway"
cfg = runtime.load_config(SERVICE)
otel.setup(SERVICE, cfg["version"])
app = runtime.create_app(SERVICE)
log = logging.getLogger(SERVICE)
client = httpx.Client()
policy = http.RetryPolicy.from_config(cfg["orders"])

_WINDOW = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


class CheckoutRequest(BaseModel):
    customer_id: int
    sku: str
    quantity: int
    region: str
    client_version: str = "web-4.2"
    delivery_window: str | None = None
    note: str = ""


def normalize_window(raw: str | None) -> str | None:
    """Keep delivery windows in HH:MM-HH:MM form; drop anything else."""
    if raw is None or not _WINDOW.match(raw):
        return None
    return raw


@app.post("/checkout", response_model=None)
def checkout(req: CheckoutRequest) -> JSONResponse | dict[str, object]:
    payload = req.model_dump()
    payload["delivery_window"] = normalize_window(req.delivery_window)
    try:
        resp = http.post(client, f"{runtime.url('orders')}/orders", payload, policy)
    except httpx.TimeoutException:
        log.error("timeout calling orders after %dms", policy.timeout_ms)
        return JSONResponse(status_code=504, content={"error": "upstream timeout"})
    except httpx.TransportError as exc:
        log.error("orders unreachable: %s: %s", type(exc).__name__, exc)
        return JSONResponse(status_code=502, content={"error": "upstream unavailable"})
    if resp.status_code >= 500:
        log.error("orders returned %d: %s", resp.status_code, resp.text[:200])
        return JSONResponse(status_code=502, content={"error": "upstream error"})
    if resp.status_code >= 400:
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    body: dict[str, object] = resp.json()
    return body


if __name__ == "__main__":
    runtime.serve(app, SERVICE)
