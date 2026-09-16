# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hot-reload semantics of the agent/team observability config sync (issue #3366).

Parameter edits inside ``agent_observability`` / ``team_observability`` while
enabled must rebuild the OTel provider on the next sync instead of being
silently ignored until restart. These tests pin that behavior, plus the
invariants the fix must not break: the no-change fast path performs no
rebuild, and a provider owned by another runtime (trajectory_ui holding the
demand) is never torn down by this runtime's rebuild cycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import jiuwenswarm.agents.harness.agent_observability as obs_mod
from jiuwenswarm.agents.harness.agent_observability import (
    shutdown_agent_observability,
    sync_agent_observability,
)

from openjiuwen.extensions.observability.setup import (
    get_config as get_active_config,
    is_initialized,
)


@pytest.fixture()
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config machinery at a throwaway data dir with a config.yaml.

    Path resolution caches at import time (``_workspace_base_dir`` /
    ``_resolve_paths``), so setting the env var alone is not enough — the
    caches must be reset for the new value to be picked up (same approach as
    ``TestMultiInstanceEnvVars`` in tests/unit_tests/test_utils.py).
    """
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "config.yaml").touch()
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data_dir))

    import jiuwenswarm.common.utils as utils

    monkeypatch.setattr(utils, "_workspace_base_dir", None, raising=False)
    monkeypatch.setattr(utils, "_user_home", None, raising=False)
    monkeypatch.setattr(utils, "_initialized", False, raising=False)
    # Repoint the module-level config path the reader uses.
    import jiuwenswarm.common.config as config_mod

    config_path = data_dir / "config" / "config.yaml"
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", config_path, raising=False)
    config_mod._YAML_PARSE_CACHE.clear()
    return config_path


@pytest.fixture()
def observability_state():
    """Reset module flags and provider before/after each test.

    ``_force_ever_enabled`` is process-sticky by design and never falls back
    on its own, so it must be reset here too — otherwise a force test leaves
    it true and every later ``disable -> provider down`` assertion fails,
    coupling tests to declaration order (same reset discipline as
    test_debug_trace.py's ``_reset()``).
    """
    obs_mod._force_ever_enabled = False
    shutdown_agent_observability()
    yield
    obs_mod._force_ever_enabled = False
    shutdown_agent_observability()


def write_agent_config(path: Path, **observability: object) -> None:
    path.write_text(
        "agent_observability:\n"
        + "".join(f"  {k}: {v}\n" for k, v in observability.items()),
        encoding="utf-8",
    )


def test_enable_from_disabled_initializes_provider(
    isolated_config: Path, observability_state
) -> None:
    write_agent_config(isolated_config, enabled=True, endpoint="http://collector-a:4317")
    sync_agent_observability()
    cfg = get_active_config()
    assert is_initialized()
    assert cfg is not None and cfg.endpoint == "http://collector-a:4317"


def test_parameter_edit_while_enabled_hot_reloads(
    isolated_config: Path, observability_state
) -> None:
    write_agent_config(isolated_config, enabled=True, endpoint="http://collector-a:4317")
    sync_agent_observability()

    write_agent_config(
        isolated_config, enabled=True, endpoint="http://collector-b:4317", service_name="svc-b"
    )
    sync_agent_observability()

    cfg = get_active_config()
    assert cfg is not None
    assert cfg.endpoint == "http://collector-b:4317"
    assert cfg.service_name == "svc-b"


def test_exporter_switch_while_enabled_hot_reloads(
    isolated_config: Path, observability_state
) -> None:
    write_agent_config(isolated_config, enabled=True, exporter="file")
    sync_agent_observability()
    assert get_active_config().exporter == "file"

    write_agent_config(isolated_config, enabled=True, exporter="otlp_grpc")
    sync_agent_observability()
    assert get_active_config().exporter == "otlp_grpc"


def test_unchanged_config_does_not_rebuild_provider(
    isolated_config: Path, observability_state
) -> None:
    write_agent_config(isolated_config, enabled=True, endpoint="http://collector-a:4317")
    sync_agent_observability()
    provider_before = get_active_config()

    sync_agent_observability()
    sync_agent_observability()

    # Same config value applied: the fast path returns without any rebuild.
    assert get_active_config() == provider_before
    assert obs_mod._last_applied_config is not None
    assert obs_mod._last_applied_config == provider_before


def test_disable_releases_provider(
    isolated_config: Path, observability_state
) -> None:
    write_agent_config(isolated_config, enabled=True)
    sync_agent_observability()
    assert is_initialized()

    write_agent_config(isolated_config, enabled=False)
    sync_agent_observability()
    assert not is_initialized()


def test_trajectory_ui_holds_provider_when_agent_disabled(
    isolated_config: Path, observability_state
) -> None:
    """trajectory_ui.enabled keeps its own demand: agent disable must not
    tear the provider down (shared-provider ownership invariant)."""
    isolated_config.write_text(
        "agent_observability:\n"
        "  enabled: true\n"
        "  endpoint: http://collector-a:4317\n"
        "trajectory_ui:\n"
        "  enabled: true\n",
        encoding="utf-8",
    )
    sync_agent_observability()
    assert is_initialized()

    isolated_config.write_text(
        "agent_observability:\n"
        "  enabled: false\n"
        "trajectory_ui:\n"
        "  enabled: true\n",
        encoding="utf-8",
    )
    sync_agent_observability()
    # The trajectory runtime still needs the provider; it must survive.
    assert is_initialized()


def test_force_flag_is_sticky_across_disable(
    isolated_config: Path, observability_state
) -> None:
    """A /debug force-enable keeps the provider up for the process lifetime
    (pre-existing sticky semantics, unchanged by the hot-reload fix)."""
    write_agent_config(isolated_config, enabled=False)
    sync_agent_observability(force=True)
    assert is_initialized()

    write_agent_config(isolated_config, enabled=False)
    sync_agent_observability()
    assert is_initialized()


def test_flags_reset_after_shutdown(
    isolated_config: Path, observability_state
) -> None:
    write_agent_config(isolated_config, enabled=True)
    sync_agent_observability()
    assert obs_mod._agent_observability_active

    shutdown_agent_observability()
    assert not obs_mod._agent_observability_active
    assert obs_mod._last_applied_config is None
