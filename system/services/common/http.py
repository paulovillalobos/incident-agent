"""Outbound HTTP with explicit retry policy.

Connection errors (the target is restarting) are always retried up to
``connect_retries`` times because the request never reached the server. Responses with a
status in ``retry_statuses`` and timeouts are retried up to ``retries`` times, waiting
``backoff_ms * 2**attempt`` between attempts.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from services.common import otel

log = logging.getLogger("services.http")
_retries = otel.meter("services.http").create_counter(
    "http.client.retries", description="Retried outbound requests"
)


@dataclass(frozen=True)
class RetryPolicy:
    timeout_ms: int
    retries: int = 0
    backoff_ms: int = 0
    connect_retries: int = 3
    connect_backoff_ms: int = 400
    retry_statuses: tuple[int, ...] = (503,)
    retry_timeouts: bool = False

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> RetryPolicy:
        return cls(
            timeout_ms=int(cfg["timeout_ms"]),
            retries=int(cfg.get("retries", 0)),
            backoff_ms=int(cfg.get("backoff_ms", 0)),
            connect_retries=int(cfg.get("connect_retries", 3)),
            connect_backoff_ms=int(cfg.get("connect_backoff_ms", 400)),
            retry_timeouts=bool(cfg.get("retry_timeouts", False)),
        )


def post(client: httpx.Client, url: str, payload: dict[str, Any], policy: RetryPolicy) -> httpx.Response:
    attempt = 0
    connect_attempt = 0
    target = url.split("/", 3)[2]
    while True:
        try:
            response = client.post(url, json=payload, timeout=policy.timeout_ms / 1000)
        except httpx.ConnectError:
            if connect_attempt >= policy.connect_retries:
                raise
            connect_attempt += 1
            _retries.add(1, {"server.address": target, "reason": "connect"})
            time.sleep(policy.connect_backoff_ms / 1000)
            continue
        except httpx.TimeoutException:
            if not policy.retry_timeouts or attempt >= policy.retries:
                raise
            reason = "timeout"
        else:
            if response.status_code not in policy.retry_statuses or attempt >= policy.retries:
                return response
            reason = f"status {response.status_code}"
        attempt += 1
        _retries.add(1, {"server.address": target, "reason": reason.split()[0]})
        log.warning("retrying POST %s (attempt %d/%d): %s", url, attempt, policy.retries, reason)
        if policy.backoff_ms:
            time.sleep(policy.backoff_ms * (2 ** (attempt - 1)) / 1000)
