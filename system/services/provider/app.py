"""Stand-in for the external payment provider (one process per region).

Not our code and not instrumented: we only see it through the client spans emitted by
payments. Latency is log-normal; ``/admin/latency`` lets a scenario degrade a region.
"""

from __future__ import annotations

import os
import random
import time
import uuid
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from services.common import runtime

INSTANCE = os.environ.get("IA_INSTANCE", "provider-eu1")
cfg = runtime.load_config("provider")
state: dict[str, float] = {"median_ms": float(cfg["median_ms"]), "sigma": float(cfg["sigma"])}
app = FastAPI(title=INSTANCE)


class Latency(BaseModel):
    median_ms: float
    sigma: float | None = None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/charges")
def charge(body: dict[str, Any]) -> dict[str, Any]:
    time.sleep(random.lognormvariate(0, state["sigma"]) * state["median_ms"] / 1000)
    return {"id": f"pr_{uuid.uuid4().hex[:12]}", "status": "succeeded"}


@app.post("/admin/latency")
def set_latency(body: Latency) -> dict[str, float]:
    state["median_ms"] = body.median_ms
    if body.sigma is not None:
        state["sigma"] = body.sigma
    return state


if __name__ == "__main__":
    runtime.serve(app, INSTANCE)
