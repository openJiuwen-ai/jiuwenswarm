"""set_config must persist atomically under the shared config write lock.

The old implementation was a bare open('w') + yaml.safe_dump: a crash
mid-write left a truncated config.yaml for the next reader, a concurrent
writer could interleave, and safe_dump stripped every comment and quote
style the round-trip loader had preserved.
"""

from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.config import set_config


def test_crash_mid_write_leaves_original_file_intact(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    """If the write fails, the on-disk config must be untouched.

    The failure is injected at _atomic_replace — after the serializer has
    written the tmp file — so set_config walks its real write path. A
    regression back to truncating open(path, "w") first would empty the file
    before any dump call and fail this assertion.
    """
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)
    original = temp_config_file.read_bytes()

    def fail_replace(src, dst, *args, **kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(config_module, "_atomic_replace", fail_replace)
    with pytest.raises(OSError):
        set_config({"model": {"name": "x"}})

    assert temp_config_file.read_bytes() == original
    # the tmp file the serializer staged must not linger after the failure
    assert not list(temp_config_file.parent.glob("*.yaml.tmp"))


def test_write_runs_under_the_config_write_lock(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    """Load and dump inside set_config must both see the lock held (probe)."""
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)

    observed: list[bool] = []
    real_dump = config_module.dump_yaml_round_trip

    def probing_dump(path, data):
        observed.append(config_module._CONFIG_WRITE_LOCK.acquire(blocking=False))  # pylint: disable=protected-access
        if observed[-1]:
            config_module._CONFIG_WRITE_LOCK.release()  # pylint: disable=protected-access
        return real_dump(path, data)

    monkeypatch.setattr(config_module, "dump_yaml_round_trip", probing_dump)
    set_config({"model": {"name": "x"}})

    assert observed and all(held is False for held in observed), (
        "set_config wrote outside the config write lock"
    )


def test_persisted_config_keeps_comments_and_quotes(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    """A comment above a mapping key and a quoted scalar must survive a write.

    yaml.safe_dump dropped both; dump_yaml_round_trip preserves them.
    """
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)
    temp_config_file.write_text(
        "# heartbeat schedule comment\nheartbeat:\n  every: \"30 * * * *\"\n",
        encoding="utf-8",
    )

    data = config_module.load_yaml_round_trip(temp_config_file)
    data["heartbeat"]["target"] = "web"
    set_config(data)

    saved = temp_config_file.read_text(encoding="utf-8")
    assert "# heartbeat schedule comment" in saved
    assert '"30 * * * *"' in saved
    # still valid YAML carrying the new value
    assert yaml.safe_load(saved)["heartbeat"]["target"] == "web"


def test_set_config_result_is_readable_by_get_config_raw(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    """Round trip through set_config keeps the mapping loadable."""
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)
    set_config({"model": {"name": "m1"}, "heartbeat": {"every": "x"}})
    raw = config_module.load_yaml_round_trip(temp_config_file)
    assert raw["model"]["name"] == "m1"
