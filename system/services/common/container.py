"""Stand-in for the container runtime's memory limit.

Services run as plain local processes here, so there is no cgroup to enforce the memory
limit of the deployment. This watchdog plays that role: when resident memory exceeds the
limit the process is killed with exit status 137, as the kernel OOM killer would do.
The supervisor then restarts it and records the event in the platform log.
"""

from __future__ import annotations

import os
import resource
import sys
import threading
import time


def peak_rss_bytes() -> int:
    """High-water mark of resident memory (not reduced by macOS memory compression)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def enforce_memory_limit(limit_mb: int, interval_s: float = 0.25) -> None:
    limit = limit_mb * 1024 * 1024

    def watch() -> None:
        while True:
            if peak_rss_bytes() > limit:
                os._exit(137)
            time.sleep(interval_s)

    threading.Thread(target=watch, name="memory-limit", daemon=True).start()
