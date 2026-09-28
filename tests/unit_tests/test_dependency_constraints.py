import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIN_PROTOBUF5_COMPATIBLE_OTEL = Version("1.28.0")
OTEL_REQUIREMENTS = {
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-exporter-otlp-proto-http",
}
LOCKED_FILELOCK = Version("3.29.0")
# The first release whose async locks can only be released by the acquiring task.
FIRST_TASK_OWNED_FILELOCK = Version("4.0.2")


def test_opentelemetry_dependencies_exclude_protobuf4_only_proto_versions():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    requirements = {
        Requirement(raw).name: Requirement(raw)
        for raw in pyproject["project"]["dependencies"]
    }

    for name in OTEL_REQUIREMENTS:
        specifier = requirements[name].specifier
        assert specifier.contains(MIN_PROTOBUF5_COMPATIBLE_OTEL), (
            f"{name} must allow OpenTelemetry {MIN_PROTOBUF5_COMPATIBLE_OTEL}"
        )
        assert not specifier.contains(Version("1.27.0")), (
            f"{name} must exclude OpenTelemetry 1.27.0 and older because "
            "opentelemetry-proto<1.28 requires protobuf<5"
        )


def test_filelock_dependency_excludes_task_owned_async_locks():
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]
    groups = {
        "dependencies": project["dependencies"],
        "harmony": project["optional-dependencies"]["harmony"],
    }

    for group, raw_requirements in groups.items():
        requirements = {Requirement(raw).name: Requirement(raw) for raw in raw_requirements}
        specifier = requirements["filelock"].specifier
        assert specifier.contains(LOCKED_FILELOCK), f"{group} must allow filelock {LOCKED_FILELOCK}"
        assert not specifier.contains(FIRST_TASK_OWNED_FILELOCK), (
            f"{group} must exclude filelock {FIRST_TASK_OWNED_FILELOCK}+: openjiuwen's FS lock "
            "releases from a different task than it acquires from"
        )


@pytest.mark.asyncio
async def test_openjiuwen_fs_lock_releases_with_installed_filelock(tmp_path, monkeypatch):
    """Run the lock sequence of a workspace reload against the installed filelock.

    Under filelock 4.0.5 the read fails with "not held by this task", the first
    write leaves its lock active ("Cannot close an active file lock") and the
    next write of the same file waits out the whole timeout, which stalled every
    request of the Session being reloaded.
    """
    from openjiuwen.core.sys_operation.local._rw_lock_manager import ReadWriteLockManager

    # The manager keeps process-wide state; give this test its own.
    for name, value in (
        ("_lock_dir", tmp_path / "locks"), ("_locks", {}), ("_state_lock", None),
        ("_cleanup_task", None), ("_idle_heap", []), ("_idle_deadlines", {}),
    ):
        monkeypatch.setattr(ReadWriteLockManager, name, value)
    marker = tmp_path / "memory" / ".workspace"
    marker.parent.mkdir()
    marker.write_text("")
    try:
        async with ReadWriteLockManager.lock_guard(marker, "read", 2):
            marker.read_text()
        for _ in range(2):
            async with ReadWriteLockManager.lock_guard(marker, "write", 2):
                marker.write_text("")
    finally:
        await ReadWriteLockManager.stop()
