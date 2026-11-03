"""Explicit cgroup policy selection must not silently change backends."""
import pytest
from pydantic import ValidationError
from jiuwenbox.models.policy import CgroupPolicy
from jiuwenbox.supervisor import cgroup


@pytest.mark.parametrize('mode,expected', [('auto', cgroup.CgroupBackend.V2), ('v1', cgroup.CgroupBackend.V1), ('v2', cgroup.CgroupBackend.V2)])
def test_policy_selects_backend_on_hybrid_host(monkeypatch, mode, expected):
    monkeypatch.setattr(cgroup, '_v1_available', lambda: True)
    monkeypatch.setattr(cgroup, '_v2_available', lambda: True)
    cgroup.detect_backend.cache_clear()
    calls = []
    monkeypatch.setattr(cgroup, '_setup_v1', lambda *args: calls.append(cgroup.CgroupBackend.V1))
    monkeypatch.setattr(cgroup, '_setup_v2', lambda *args: calls.append(cgroup.CgroupBackend.V2))
    try:
        cgroup.setup('test-backend', CgroupPolicy(backend=mode, memory_max=1024))
        assert calls == [expected]
    finally:
        cgroup.detect_backend.cache_clear()


@pytest.mark.parametrize('mode', ['v1', 'v2'])
def test_forced_backend_unavailable_is_an_error(monkeypatch, mode):
    monkeypatch.setattr(cgroup, '_v1_available', lambda: mode != 'v1')
    monkeypatch.setattr(cgroup, '_v2_available', lambda: mode != 'v2')
    with pytest.raises(cgroup.CgroupSetupError, match=mode):
        cgroup.setup('test-backend', CgroupPolicy(backend=mode, memory_max=1024))


def test_backend_does_not_enable_limits_by_itself():
    assert CgroupPolicy(backend='v1').is_empty()
    assert CgroupPolicy().backend == 'auto'
    with pytest.raises(ValidationError):
        CgroupPolicy(backend='unknown')
