"""Read-only metrics for owned application service trees, never arbitrary PIDs."""
import time
from pathlib import Path
from typing import Any

from .app_market.models import AppInstance

import psutil


class AppServiceResources:
    def __init__(self) -> None:
        self._previous = {}
        self._snapshot = None
        self._sample_time = 0.0
        self._identities = ()

    def sample(
        self, instances: list[AppInstance],
    ) -> list[dict[str, Any]]:
        active = [item for item in instances if item.state in ("running", "starting", "stopping")]
        identities = tuple((item.instance_id, item.pid) for item in active)
        now = time.monotonic()
        if self._snapshot is not None and identities == self._identities and now - self._sample_time < 2.5:
            return self._snapshot
        previous = self._previous
        current = {}
        apps = []
        cores = psutil.cpu_count() or 1
        for instance in active:
            reading = {"appId": instance.app_id, "cpuPercent": None, "memoryBytes": None}
            try:
                # A persisted PID alone is not proof of ownership after a restart.
                if not instance.pid or not instance.status_token:
                    apps.append(reading)
                    continue
                root = psutil.Process(instance.pid)
                environment = root.environ()
                if (environment.get("WORKSWARM_STATUS_TOKEN") != instance.status_token
                        or Path(environment.get("WORKSWARM_INSTANCE_CONTEXT", "")).name != f"{instance.instance_id}.json"):
                    apps.append(reading)
                    continue
                processes = [root, *root.children(recursive=True)]
                cpu = 0.0
                memory = 0
                warm = True
                for process in processes:
                    with process.oneshot():
                        identity = (process.pid, process.create_time())
                        times = process.cpu_times()
                        seconds = times.user + times.system
                        memory += process.memory_info().rss
                    if not process.is_running():
                        raise psutil.NoSuchProcess(process.pid)
                    current[identity] = (now, seconds)
                    old = previous.get(identity)
                    if old is None:
                        warm = False
                    else:
                        cpu += max(0.0, seconds - old[1]) / max(now - old[0], 0.001) * 100 / cores
                if not root.is_running():
                    apps.append(reading)
                    continue
                reading.update(cpuPercent=cpu if warm else None, memoryBytes=memory)
            except (psutil.Error, OSError):
                # A disappearing/denied child makes the tree incomplete, not zero.
                pass
            apps.append(reading)
        self._previous = current
        self._sample_time = now
        self._identities = identities
        self._snapshot = apps
        return apps


class SystemResources:
    """Sample the service host without thread-local CPU baselines or timers."""

    def __init__(self) -> None:
        self._previous = None
        self._snapshot = None
        self._sample_time = 0.0

    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._snapshot is not None and now - self._sample_time < 2.5:
            return self._snapshot
        reading = {"cpuPercent": None, "memoryPercent": None, "sampledAt": time.time() * 1000}
        try:
            times = psutil.cpu_times()
            # Windows interrupt/DPC are already included in system; Linux guest in user/nice.
            total = sum(getattr(times, key, 0) for key in
                        ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"))
            idle = times.idle + getattr(times, "iowait", 0)
            if self._previous is not None:
                elapsed = total - self._previous[0]
                idle_delta = idle - self._previous[1]
                if elapsed > 0 and idle_delta >= 0:
                    reading["cpuPercent"] = min(100.0, max(0.0, (1 - idle_delta / elapsed) * 100))
            self._previous = (total, idle)
        except (psutil.Error, OSError):
            self._previous = None
        try:
            reading["memoryPercent"] = psutil.virtual_memory().percent
        except (psutil.Error, OSError):
            pass
        self._snapshot = reading
        self._sample_time = now
        return reading
