"""External CLI path history writes must serialize under the config write lock.

``update_external_cli_path_histories_in_config`` (AgentServer-side, config
panel saves) and the gateway-side prune both read-modify-write config.yaml
across two processes. Each function's RMW section must hold the shared write
boundary; concurrent writers otherwise interleave load/dump and lose updates.
"""

import threading
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.config import (
    prune_external_cli_path_histories_in_config,
    update_external_cli_path_histories_in_config,
)


def _write_history(path: Path, entries: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"modes": {"team": {"jiuwen_team": {"external_cli_path_histories": {"codex": entries}}}}}),
        encoding="utf-8",
    )


def _load_history(path: Path) -> list[str]:
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    return saved["modes"]["team"]["jiuwen_team"]["external_cli_path_histories"].get("codex", [])


def test_concurrent_updates_serialize_last_write_wins_whole(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    """Interleaved full-history updates must never persist a partial list.

    Each update's load-dump is atomic under the lock, so the file always
    holds one writer's complete history, never a merge of two writers'
    in-flight states (e.g. duplicated or reordered entries mid-list).
    """
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)
    _write_history(temp_config_file, ["p0"])

    rounds = 12
    writes: list[tuple[str, ...]] = []
    lock = threading.Lock()

    def writer(seed: int) -> None:
        for i in range(rounds):
            payload = [f"p{seed}-{j}" for j in range(i + 1)]
            update_external_cli_path_histories_in_config({"codex": payload})
            with lock:
                writes.append(tuple(payload))

    t1 = threading.Thread(target=writer, args=(1,))
    t2 = threading.Thread(target=writer, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    final = _load_history(temp_config_file)
    # The file must equal one of the writers' complete payloads (the last
    # serialized write), possibly truncated to the tail window by the
    # EXTERNAL_CLI_PATH_HISTORY_LIMIT — but never a torn interleave of two
    # writers' lists.
    limit = config_module.EXTERNAL_CLI_PATH_HISTORY_LIMIT
    all_forms = set(writes) | {payload[-limit:] for payload in writes}
    assert tuple(final) in all_forms, (
        f"persisted history is not any writer's (possibly tail-truncated) payload: {final}"
    )


def test_prune_waits_for_inflight_update(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    """A prune that runs concurrently with an update must not tear the file.

    Both hold the write lock across load-dump, so the final file reflects one
    complete serialized outcome: either the update's full list, or the prune's
    survivors computed from a state that already includes the update.
    """
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)
    _write_history(temp_config_file, ["/seed", "/stale"])

    update_started = threading.Event()
    prune_done = threading.Event()

    def concurrent_update() -> None:
        update_started.wait()
        update_external_cli_path_histories_in_config(
            {"codex": ["/seed", "/stale", "/just-replaced"]}
        )

    def prune() -> None:
        update_started.set()
        prune_external_cli_path_histories_in_config("codex", ["/stale"])
        prune_done.set()

    updater = threading.Thread(target=concurrent_update)
    updater.start()
    prune_thread = threading.Thread(target=prune)
    prune_thread.start()
    updater.join(timeout=30)
    prune_done.wait(timeout=30)

    final = _load_history(temp_config_file)
    # Complete serialized outcomes (update-then-prune, prune-then-update,
    # or the update alone if the prune saw nothing to drop):
    valid = (
        ["/seed", "/just-replaced"],           # update first, then prune
        ["/seed", "/stale", "/just-replaced"],  # prune saw pre-update state
    )
    assert final in valid, f"torn or interleaved result: {final}"


def test_write_lock_held_during_load_and_dump(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    """The RMW section must hold the global threading lock end-to-end.

    If the load and dump were outside the lock, a second thread could acquire
    the lock between them; assert the lock stays held across the whole write.
    """
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)
    _write_history(temp_config_file, ["p0"])

    observations: list[bool] = []
    real_load = config_module.load_yaml_round_trip
    real_dump = config_module.dump_yaml_round_trip

    def probing_load(path):
        observations.append(config_module._CONFIG_WRITE_LOCK.acquire(blocking=False))  # pylint: disable=protected-access
        if observations[-1]:
            config_module._CONFIG_WRITE_LOCK.release()  # pylint: disable=protected-access
        return real_load(path)

    def probing_dump(path, data):
        observations.append(config_module._CONFIG_WRITE_LOCK.acquire(blocking=False))  # pylint: disable=protected-access
        if observations[-1]:
            config_module._CONFIG_WRITE_LOCK.release()  # pylint: disable=protected-access
        return real_dump(path, data)

    monkeypatch.setattr(config_module, "load_yaml_round_trip", probing_load)
    monkeypatch.setattr(config_module, "dump_yaml_round_trip", probing_dump)

    update_external_cli_path_histories_in_config({"codex": ["p0", "p1"]})

    # Both the load and the dump ran while another thread could NOT grab the lock.
    assert observations and all(held is False for held in observations), (
        "load/dump executed outside the write lock"
    )
    assert _load_history(temp_config_file) == ["p0", "p1"]
