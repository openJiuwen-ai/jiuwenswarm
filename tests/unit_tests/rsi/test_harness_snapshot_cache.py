"""Read-only snapshot caching must preserve freshness and projection isolation."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rsi.harness_provider import HarnessProvider


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "task" / "run" / "single_harness_state.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("status: running\nnested: {score: 0}\n", encoding="utf-8")
    return HarnessProvider(tmp_path), path


def read_snapshot(provider):
    return provider._load_yaml("task", "single_harness_state.yaml")


def test_unchanged_snapshot_parsed_once_and_isolated(snapshot):
    provider, _ = snapshot
    with patch.object(yaml, "safe_load", wraps=yaml.safe_load) as parse:
        first = read_snapshot(provider)
        first["nested"]["score"] = 1
        assert read_snapshot(provider)["nested"]["score"] == 0
        assert parse.call_count == 1


def test_concurrent_readers_share_parse(snapshot):
    provider, _ = snapshot
    with patch.object(yaml, "safe_load", wraps=yaml.safe_load) as parse:
        with ThreadPoolExecutor(max_workers=4) as pool:
            states = list(pool.map(lambda _: read_snapshot(provider), range(16)))
        assert all(state["status"] == "running" for state in states)
        assert parse.call_count == 1


def test_rewrite_replacement_and_deletion_invalidate(snapshot):
    provider, path = snapshot
    read_snapshot(provider)
    path.write_text("status: completed\n", encoding="utf-8")
    assert read_snapshot(provider) == {"status": "completed"}
    replacement = path.with_suffix(".tmp")
    replacement.write_text("status: replaced\n", encoding="utf-8")
    replacement.replace(path)
    assert read_snapshot(provider) == {"status": "replaced"}
    path.unlink()
    assert read_snapshot(provider) is None
    path.write_text("status: recreated\n", encoding="utf-8")
    assert read_snapshot(provider) == {"status": "recreated"}


def test_invalid_yaml_does_not_return_stale_snapshot(snapshot):
    provider, path = snapshot
    read_snapshot(provider)
    path.write_text("status: [\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        read_snapshot(provider)
    path.write_text("status: recovered\n", encoding="utf-8")
    assert read_snapshot(provider) == {"status": "recovered"}


def test_changed_during_parse_is_not_cached(snapshot):
    provider, path = snapshot
    original = yaml.safe_load

    def parse_and_replace(stream):
        result = original(stream)
        path.write_text("status: completed\n", encoding="utf-8")
        return result

    with patch.object(yaml, "safe_load", side_effect=parse_and_replace):
        assert read_snapshot(provider)["status"] == "running"
    assert not provider._snapshot_cache
    assert read_snapshot(provider)["status"] == "completed"


def test_cache_is_bounded_and_evicts_least_recently_used(snapshot):
    provider, path = snapshot
    read_snapshot(provider)
    for index in range(32):
        sibling = path.with_name(f"report-{index}.yaml")
        sibling.write_text("score: 0\n", encoding="utf-8")
        provider._load_yaml("task", sibling.name)
    assert len(provider._snapshot_cache) == 32
    assert path not in provider._snapshot_cache


@pytest.mark.parametrize("content, expected", [("", {}), ("- item\n", None)])
def test_empty_and_non_mapping_snapshots(snapshot, content, expected):
    provider, path = snapshot
    path.write_text(content, encoding="utf-8")
    assert read_snapshot(provider) == expected
    assert read_snapshot(provider) == expected
