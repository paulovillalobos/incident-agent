"""Runs the services of the system as local processes, like a tiny container orchestrator.

Each service runs from a system tree (``config/``, ``migrations/``, ``services/``) with
environment variables for ports, data directory and OTLP endpoint. Deploys are rolling:
the new process binds the same port (SO_REUSEPORT), and the old one is stopped only
after the new one reports ready. Processes that exit unexpectedly are restarted (restart
policy "always") and the event is written to the platform log, which is how an OOM kill
shows up in the telemetry.
"""

from __future__ import annotations

import itertools
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from incident_agent.runner.platform_log import PlatformLog


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    module: str
    http: bool = True


SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec("provider-eu1", "services.provider.app"),
    ServiceSpec("provider-eu2", "services.provider.app"),
    ServiceSpec("payments-a", "services.payments.app"),
    ServiceSpec("payments-b", "services.payments.app"),
    ServiceSpec("inventory", "services.inventory.app"),
    ServiceSpec("orders", "services.orders.app"),
    ServiceSpec("gateway", "services.gateway.app"),
    ServiceSpec("worker", "services.worker.main", http=False),
)


def allocate_ports(base: int) -> dict[str, int]:
    names = [s.name for s in SERVICES if s.http]
    ports = {name: base + i for i, name in enumerate(names)}
    for name, port in ports.items():
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"port {port} for {name} is busy") from exc
    return ports


@dataclass
class _Proc:
    popen: subprocess.Popen[bytes]
    ready_file: Path


class Supervisor:
    def __init__(
        self,
        tree: Path,
        data_dir: Path,
        log_dir: Path,
        ports: dict[str, int],
        otlp_endpoint: str,
        platform: PlatformLog,
    ) -> None:
        self.tree = tree
        self.data_dir = data_dir
        self.log_dir = log_dir
        self.ports = ports
        self.otlp_endpoint = otlp_endpoint
        self.platform = platform
        self.specs = {s.name: s for s in SERVICES}
        self.procs: dict[str, _Proc] = {}
        self._logs: dict[str, IO[bytes]] = {}
        self._lock = threading.RLock()
        self._closing = False
        self._seq = itertools.count()
        self.restarts: list[tuple[float, str, int]] = []
        self._monitor = threading.Thread(target=self._watch, name="supervisor", daemon=True)
        (data_dir / "ready").mkdir(parents=True, exist_ok=True)

    def _spawn(self, name: str) -> _Proc:
        spec = self.specs[name]
        log = self._logs.get(name)
        if log is None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log = (self.log_dir / f"{name}.log").open("ab")
            self._logs[name] = log
        ready = self.data_dir / "ready" / f"{name}.{next(self._seq)}"
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": str(self.tree),
                "PYTHONDONTWRITEBYTECODE": "1",
                "IA_SYSTEM_ROOT": str(self.tree),
                "IA_DATA_DIR": str(self.data_dir),
                "IA_PORTS": json.dumps(self.ports),
                "IA_OTLP_ENDPOINT": self.otlp_endpoint,
                "IA_INSTANCE": name,
                "IA_READY_FILE": str(ready),
            }
        )
        popen = subprocess.Popen(
            [sys.executable, "-m", spec.module],
            cwd=self.tree,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        return _Proc(popen, ready)

    def _wait_ready(self, name: str, proc: _Proc, timeout_s: float = 90.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if proc.ready_file.exists():
                return
            if proc.popen.poll() is not None:
                raise RuntimeError(f"{name} exited during startup (see {self.log_dir / name}.log)")
            time.sleep(0.05)
        raise RuntimeError(f"{name} not ready after {timeout_s}s (see {self.log_dir / name}.log)")

    def start_all(self) -> None:
        for spec in SERVICES:
            proc = self._spawn(spec.name)
            self._wait_ready(spec.name, proc)
            with self._lock:
                self.procs[spec.name] = proc
        self._monitor.start()

    def restart(self, name: str, reason: str) -> None:
        """Rolling restart (a deploy): start the new version, wait ready, stop the old one."""
        new = self._spawn(name)
        self._wait_ready(name, new)
        with self._lock:
            old = self.procs[name]
            self.procs[name] = new
        self._terminate(old.popen)
        self.platform.emit("platform", f"rollout of {name} complete ({reason})")

    @staticmethod
    def _terminate(proc: subprocess.Popen[bytes], grace_s: float = 6.0) -> None:
        if proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _watch(self) -> None:
        while not self._closing:
            time.sleep(0.2)
            with self._lock:
                for name, proc in list(self.procs.items()):
                    code = proc.popen.poll()
                    if code is None or self._closing:
                        continue
                    reason = " (OOMKilled: memory limit exceeded)" if code == 137 else ""
                    self.platform.emit(
                        "platform",
                        f"container {name} exited with code {code}{reason}; restarting",
                        severity="ERROR",
                    )
                    self.restarts.append((time.time(), name, code))
                    self.procs[name] = self._spawn(name)

    def stop_all(self) -> None:
        self._closing = True
        if self._monitor.is_alive():
            self._monitor.join(timeout=2)
        with self._lock:
            for spec in reversed(SERVICES):
                proc = self.procs.get(spec.name)
                if proc is not None:
                    self._terminate(proc.popen)
            for log in self._logs.values():
                log.close()
