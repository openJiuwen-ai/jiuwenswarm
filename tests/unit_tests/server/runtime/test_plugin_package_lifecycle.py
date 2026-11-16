"""Thread-boundary admission for plugin execution and replacement."""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwenswarm.server.runtime import plugin_package_lifecycle as lifecycle


@pytest.mark.parametrize("first", ["reader", "writer"])
def test_plugin_reservation_registration_is_atomic_across_threads(monkeypatch, first):
    registering = threading.Event()
    allow_registration = threading.Event()
    held = threading.Event()
    release = threading.Event()
    competitor_started = threading.Event()
    monkeypatch.setattr(lifecycle, "_package_key", lambda name: name)

    def pause_registration():
        registering.set()
        assert allow_registration.wait(5)

    class Readers(Counter):
        def update(self, iterable=None):
            if iterable is not None and first == "reader":
                pause_registration()
            return super().update(iterable)

    class Writers(set):
        def add(self, name):
            if first == "writer":
                pause_registration()
            return super().add(name)

    monkeypatch.setattr(lifecycle, "_users", Readers())
    monkeypatch.setattr(lifecycle, "_writers", Writers())
    reader = lambda: lifecycle.use_plugins(["test"])
    writer = lambda: lifecycle.replace_plugin("test")
    acquire, compete = (reader, writer) if first == "reader" else (writer, reader)

    def hold():
        with acquire():
            held.set()
            assert release.wait(5)

    def attempt_competing_reservation():
        competitor_started.set()
        with pytest.raises(lifecycle.PluginPackageBusy):
            with compete():
                pytest.fail("overlapping reader and writer reservations")

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(hold)
        try:
            assert registering.wait(5)
            competitor = pool.submit(attempt_competing_reservation)
            assert competitor_started.wait(5)
            with pytest.raises(TimeoutError):
                competitor.result(timeout=0.05)
            allow_registration.set()
            assert held.wait(5)
            competitor.result(timeout=5)
        finally:
            allow_registration.set()
            release.set()
        owner.result(timeout=5)
    assert not lifecycle._users
    assert not lifecycle._writers
