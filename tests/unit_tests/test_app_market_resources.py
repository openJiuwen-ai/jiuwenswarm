"""Resource identity, sampling, and account isolation checks."""
import asyncio
from types import SimpleNamespace

import psutil
import pytest

from jiuwenswarm.extensions.app_market.manager import AppMarketManager
from jiuwenswarm.extensions.app_market.resources import AppServiceResources, SystemResources


def instance(app_id="one", pid=12, owner="alice"):
    return SimpleNamespace(instance_id=app_id, app_id=app_id, pid=pid, state="running", owner_id=owner, status_token=app_id)


class Process:
    def __init__(self, pid, seconds=1, memory=100):
        self.pid = pid
        self.seconds = seconds
        self.memory = memory
        self.created = 1
        self.descendants = []
        self.token = "one"
        self.context = "one.json"
        self.alive = True

    def environ(self):
        return {"WORKSWARM_STATUS_TOKEN": self.token, "WORKSWARM_INSTANCE_CONTEXT": self.context}

    def is_running(self):
        return self.alive

    def children(self, recursive):
        return self.descendants

    def oneshot(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def create_time(self):
        return self.created

    def cpu_times(self):
        return SimpleNamespace(user=self.seconds, system=0)

    def memory_info(self):
        return SimpleNamespace(rss=self.memory)


def test_service_tree_sampling_cache_and_pid_reuse(monkeypatch):
    clock = [10]
    monkeypatch.setattr("jiuwenswarm.extensions.app_market.resources.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(psutil, "cpu_count", lambda: 4)
    root = Process(12)
    child = Process(13, memory=50)
    root.descendants = [child]
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    monitor = AppServiceResources()
    first = monitor.sample([instance()])
    assert first[0]["cpuPercent"] is None
    assert first[0]["memoryBytes"] == 150
    assert monitor.sample([instance()]) is first
    clock[0] += 3
    root.seconds += 3
    child.seconds += 3
    assert monitor.sample([instance()])[0]["cpuPercent"] == 50
    clock[0] += 3
    child.created += 1
    assert monitor.sample([instance()])[0]["cpuPercent"] is None
    assert monitor.sample([]) == []
    assert monitor._previous == {}


def test_unowned_or_inaccessible_process_is_unavailable_not_zero(monkeypatch):
    def denied(pid):
        raise psutil.AccessDenied(pid)
    monkeypatch.setattr(psutil, "Process", denied)
    reading = AppServiceResources().sample([instance()])[0]
    assert reading == {"appId": "one", "cpuPercent": None, "memoryBytes": None}


def test_manager_filters_app_resources_by_owner(tmp_path):
    manager = AppMarketManager(str(tmp_path))
    manager._service.list_instances = lambda: [instance(), instance("other", 13, "bob")]
    manager._resources.sample = lambda *args: [
        {"appId": "one", "cpuPercent": 2, "memoryBytes": 100},
        {"appId": "other", "cpuPercent": 90, "memoryBytes": 900},
    ]
    result = asyncio.run(manager.resources("alice"))
    assert len(result["apps"]) == 1
    assert result["apps"] == [{"appId": "one", "cpuPercent": 2, "memoryBytes": 100}]
    assert "total" not in result


@pytest.mark.asyncio
async def test_real_market_service_reports_memory_and_releases_metrics_after_stop(tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<html><title>Resource test</title></html>", encoding="utf-8")
    manager = AppMarketManager(str(tmp_path / "market"))
    published = await manager.publish_app(str(page), "resource-test", "alice")
    started = await manager.start_app(published["app_id"], "alice")
    assert started is not None
    try:
        # A fresh manager has no Popen records for the surviving application.
        restarted = AppMarketManager(str(tmp_path / "market"))
        first = await restarted.resources("alice")
        assert first["apps"][0]["memoryBytes"] > 0
        await asyncio.sleep(3)
        second = await restarted.resources("alice")
        assert second["apps"][0]["cpuPercent"] >= 0
        assert (await manager.resources("bob"))["apps"] == []
    finally:
        await manager.stop_app(started["instance_id"], "alice")
    assert (await manager.resources("alice"))["apps"] == []


@pytest.mark.parametrize("mismatch", ["token", "context", "alive"])
def test_stale_instance_or_reused_process_is_not_attributed(monkeypatch, mismatch):
    process = Process(12)
    setattr(process, mismatch, False if mismatch == "alive" else "other")
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    reading = AppServiceResources().sample([instance()])[0]
    assert reading["memoryBytes"] is None
    assert reading["cpuPercent"] is None


def test_pid_reused_by_another_account_only_counts_matching_instance(monkeypatch):
    process = Process(12)
    process.token = "new"
    process.context = "new.json"
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    readings = AppServiceResources().sample([instance("old"), instance("new", owner="bob")])
    assert readings[0]["memoryBytes"] is None
    assert readings[1]["memoryBytes"] == 100


def test_system_usage_has_own_cpu_baseline_and_cache(monkeypatch):
    clock = [10]
    times = SimpleNamespace(user=100, system=100, idle=800, interrupt=20, dpc=10)
    monkeypatch.setattr("jiuwenswarm.extensions.app_market.resources.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(psutil, "cpu_times", lambda: times)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(percent=62.5))
    sampler = SystemResources()
    first = sampler.sample()
    assert first["cpuPercent"] is None
    assert first["memoryPercent"] == 62.5
    assert sampler.sample() is first
    clock[0] += 3
    times.system += 60
    times.idle += 40
    times.interrupt += 30  # Already counted in Windows system time.
    assert sampler.sample()["cpuPercent"] == pytest.approx(60)
    clock[0] += 3
    times.system = times.user = times.idle = 0
    assert sampler.sample()["cpuPercent"] is None


def test_system_failure_does_not_reuse_old_numbers(monkeypatch):
    def unavailable():
        raise OSError("unavailable")
    monkeypatch.setattr(psutil, "cpu_times", unavailable)
    monkeypatch.setattr(psutil, "virtual_memory", unavailable)
    result = SystemResources().sample()
    assert result["cpuPercent"] is None
    assert result["memoryPercent"] is None


def test_system_metrics_work_without_running_apps(tmp_path):
    manager = AppMarketManager(str(tmp_path))
    result = asyncio.run(manager.resources("alice"))
    assert result["apps"] == []
    assert 0 <= result["system"]["memoryPercent"] <= 100


@pytest.mark.parametrize("guest,guest_nice", [(90, 0), (0, 90), (60, 30)])
def test_linux_guest_cpu_is_counted_once(monkeypatch, guest, guest_nice):
    clock = [10]
    times = SimpleNamespace(user=100, nice=100, system=0, idle=800, guest=0, guest_nice=0)
    monkeypatch.setattr("jiuwenswarm.extensions.app_market.resources.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(psutil, "cpu_times", lambda: times)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(percent=50))
    sampler = SystemResources()
    assert sampler.sample()["cpuPercent"] is None
    clock[0] += 3
    # Linux guest counters overlap user/nice; 90 busy + 10 idle is 90% CPU.
    times.user += guest
    times.nice += guest_nice
    times.guest += guest
    times.guest_nice += guest_nice
    times.idle += 10
    assert sampler.sample()["cpuPercent"] == pytest.approx(90)
