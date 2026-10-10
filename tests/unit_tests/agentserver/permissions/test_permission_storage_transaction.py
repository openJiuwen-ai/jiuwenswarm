"""Permission storage serialization against real YAML files and SDK policy checks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import multiprocessing
from pathlib import Path
import queue
import threading

import pytest

from jiuwenswarm.common import config
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_persist as persist
from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
    compose_host_effective_permissions,
)
from openjiuwen.harness.security.tiered_policy import evaluate_tiered_policy
from openjiuwen.harness.security.models import PermissionLevel


def _configure(root):
    root = Path(root)
    config.CONFIG_YAML_PATH = root / "config.yaml"
    config._CONFIG_YAML_PATH = config.CONFIG_YAML_PATH
    layers.user_permissions_path = lambda: root / "user_permissions.yaml"
    layers.session_permissions_path = lambda sid: root / sid / "session_permissions.yaml"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "get_config_file", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(layers, "user_permissions_path", lambda: tmp_path / "user_permissions.yaml")
    monkeypatch.setattr(layers, "session_permissions_path", lambda sid: tmp_path / sid / "session_permissions.yaml")
    config.dump_yaml_round_trip(config.CONFIG_YAML_PATH, {
        "unrelated": "global-kept",
        "permissions": {
            "enabled": True, "defaults": {"*": "ask"},
            "tools": {"bash": "ask"}, "file_guard": {"enabled": False},
        },
    })
    assert layers.save_user_permissions({"custom": "user-kept"})
    return tmp_path


def _exact(root):
    return persist.persist_exact_permission_allow_rule(
        "bash", {"command": "git status"}, session_id="session", workspace_root=root,
    )


def _effective():
    return compose_host_effective_permissions(
        global_permissions=config.load_yaml_round_trip(config.CONFIG_YAML_PATH)["permissions"],
        user_permissions=layers.load_user_permissions(),
        session_permissions=layers.load_session_permissions("session"),
    )


def _run_writer(root, operation, ready, release, result, started=None):
    """A spawned worker uses the same storage owner and waits inside its mutation."""
    _configure(root)
    if started is not None:
        started.set()

    def gate():
        if ready is not None:
            ready.set()
            if not release.wait(30):
                raise TimeoutError("test writer was not released")

    try:
        if operation == "exact":
            dump = layers._dump_yaml_dict

            def gated_dump(path, data):
                gate()
                return dump(path, data)

            layers._dump_yaml_dict = gated_dump
            try:
                outcome = _exact(root)
            finally:
                layers._dump_yaml_dict = dump
        elif operation == "global":
            def mutate(data):
                gate()
                data["permissions"]["tools"]["bash"] = "deny"
                return data
            config.update_config(mutate)
            outcome = True
        else:
            def mutate(data):
                gate()
                data["deny_tools"] = ["bash"]
                return data
            outcome = layers.update_user_permissions(mutate)
        result.put((operation, outcome))
    except BaseException as exc:
        result.put((operation, repr(exc)))


@pytest.mark.parametrize("deny_layer", ["global", "user"])
@pytest.mark.parametrize("first", ["deny", "exact"])
@pytest.mark.parametrize("worker_kind", ["thread", "process"])
def test_cooperating_writers_serialize_both_commit_orders(storage, deny_layer, first, worker_kind):
    ctx = multiprocessing.get_context("spawn")
    event = ctx.Event if worker_kind == "process" else threading.Event
    worker = ctx.Process if worker_kind == "process" else threading.Thread
    ready, release, started = event(), event(), event()
    results = ctx.Queue() if worker_kind == "process" else queue.Queue()
    before = "exact" if first == "exact" else deny_layer
    after = deny_layer if first == "exact" else "exact"
    processes = [
        worker(target=_run_writer, args=(storage, before, ready, release, results)),
        worker(target=_run_writer, args=(storage, after, None, release, results, started)),
    ]
    try:
        processes[0].start()
        assert ready.wait(30), "first process did not reach locked mutation"
        processes[1].start()
        assert started.wait(30)
        release.set()
        for process in processes:
            process.join(30)
            assert not process.is_alive()
            if worker_kind == "process":
                assert process.exitcode == 0
        outcomes = dict(results.get(timeout=5) for _ in processes)
        assert outcomes == {deny_layer: True, "exact": first == "exact"}
        assert bool(layers.load_user_permissions().get("approval_overrides")) == (first == "exact")
        assert layers.load_user_permissions()["custom"] == "user-kept"
        assert evaluate_tiered_policy(_effective(), "bash", {"command": "git status"})[0] == PermissionLevel.DENY
    finally:
        release.set()
        for process in processes:
            if worker_kind == "process" and process.pid and process.is_alive():
                process.terminate()
                process.join(5)


def test_thread_rmw_does_not_lose_unrelated_user_fields(storage):
    ready, release = threading.Event(), threading.Event()

    def first(data):
        ready.set()
        assert release.wait(5)
        data["another"] = "kept"
        return data

    def tools_set():
        from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
            dispatch_permissions_config_request,
        )
        from jiuwenswarm.common.schema.agent import AgentRequest
        from jiuwenswarm.common.schema.message import ReqMethod
        return dispatch_permissions_config_request(AgentRequest(
            request_id="storage-test",
            req_method=ReqMethod.PERMISSIONS_TOOLS_SET,
            params={"tools": {"different": "deny"}},
        ))

    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(layers.update_user_permissions, first)
        assert ready.wait(5)
        two = executor.submit(tools_set)
        release.set()
        assert one.result(timeout=10)
        response = two.result(timeout=10)
        assert response.ok
    saved = layers.load_user_permissions()
    assert saved["custom"] == "user-kept" and saved["another"] == "kept"
    assert saved["deny_tools"] == ["different"]


def test_exact_does_not_promote_session_grants(storage):
    config.update_config(lambda data: {**data, "permissions": {
        **data["permissions"], "tools": {}, "defaults": {"*": "ask"},
    }})
    assert layers.persist_session_overlay_from_effective("session", {"allow_tools": ["custom_tool"]})
    before = layers.user_permissions_path().read_bytes()
    assert not persist.persist_exact_permission_allow_rule(
        "custom_tool", {}, session_id="session", workspace_root=storage,
    )
    assert layers.user_permissions_path().read_bytes() == before


def test_exact_is_idempotent_for_an_existing_durable_grant(storage):
    assert _exact(storage)
    before = layers.user_permissions_path().read_bytes()
    assert _exact(storage)
    assert layers.user_permissions_path().read_bytes() == before


def test_exact_write_failure_preserves_document(storage, monkeypatch):
    before = layers.user_permissions_path().read_bytes()

    def fail_replace(*_args):
        raise OSError("injected write failure")

    monkeypatch.setattr(config, "_atomic_replace", fail_replace)
    assert not _exact(storage)
    assert layers.user_permissions_path().read_bytes() == before
    assert not list(storage.glob("*.yaml.tmp"))


def test_exact_lock_timeout_preserves_document(storage, monkeypatch):
    original = layers.permission_storage_lock

    @contextmanager
    def short_lock(session_id):
        with original(session_id, lock_timeout=0.02):
            yield

    monkeypatch.setattr(layers, "permission_storage_lock", short_lock)
    before = layers.user_permissions_path().read_bytes()
    with config.config_write_lock():
        assert not _exact(storage)
    assert layers.user_permissions_path().read_bytes() == before


def test_exact_rejects_unreadable_current_policy(storage):
    layers.user_permissions_path().write_text("broken: [", encoding="utf-8")
    assert not _exact(storage)
    assert layers.user_permissions_path().read_text() == "broken: ["


def test_exact_path_increment_retains_other_user_and_session_paths(storage):
    workspace = storage / "workspace"
    workspace.mkdir()
    approved = (storage / "approved.txt").as_posix()
    temporary = (storage / "temporary.txt").as_posix()
    config.update_config(lambda data: {**data, "permissions": {
        "enabled": True, "defaults": {"*": "allow"}, "tools": {},
        "file_guard": {"enabled": True, "defaults": {"read": "ask", "write": "ask", "exec": "ask"}},
    }})
    assert layers.persist_session_overlay_from_effective("session", {
        "file_guard": {"paths": [{"path": temporary, "read": "allow", "write": "ask", "exec": "ask"}]},
    })
    before = config.CONFIG_YAML_PATH.read_bytes()
    assert persist.persist_exact_permission_allow_rule(
        "read_file", {"path": approved}, ((approved, "read"),),
        session_id="session", workspace_root=workspace,
    )
    paths = layers.load_user_permissions()["file_guard"]["paths"]
    assert [item["path"] for item in paths] == [approved]
    assert paths[0]["read"] == "allow" and paths[0]["write"] == "ask"
    assert config.CONFIG_YAML_PATH.read_bytes() == before
    assert layers.load_user_permissions()["custom"] == "user-kept"


def test_rpc_reports_write_failure(storage, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import dispatch_permissions_config_request

    monkeypatch.setattr(layers, "_dump_yaml_dict", lambda *_: False)
    response = dispatch_permissions_config_request(AgentRequest(
        request_id="failure", req_method=ReqMethod.PERMISSIONS_TOOLS_SET,
        params={"tools": {"bash": "deny"}},
    ))
    assert not response.ok


def test_capture_layers_is_fresh_and_independent(storage):
    global_perms, user, session, effective = layers.capture_permission_layers("session")
    effective["tools"]["bash"] = "allow"
    user["custom"] = "changed"
    assert global_perms["tools"]["bash"] == "ask"
    assert session == {}
    assert layers.load_user_permissions()["custom"] == "user-kept"
    config.update_permissions_tool_in_config("bash", "deny")
    assert layers.capture_permission_layers("session")[3]["tools"]["bash"] == "deny"


def test_overlay_file_lock_timeout_releases_global_lock(storage, monkeypatch):
    import portalocker

    original = layers.permission_storage_lock

    @contextmanager
    def short_lock(session_id):
        with original(session_id, lock_timeout=0.02):
            yield

    monkeypatch.setattr(layers, "permission_storage_lock", short_lock)
    before = layers.user_permissions_path().read_bytes()
    with portalocker.Lock(str(config._config_lock_path(layers.user_permissions_path()))):
        assert not _exact(storage)
    assert layers.user_permissions_path().read_bytes() == before
    assert config._CONFIG_WRITE_LOCK.acquire(blocking=False)
    config._CONFIG_WRITE_LOCK.release()
