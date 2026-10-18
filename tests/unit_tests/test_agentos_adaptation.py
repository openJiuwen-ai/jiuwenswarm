"""Focused regression checks for AgentOS boundaries and hot configuration."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jiuwenswarm.common.etcd.client import EtcdError, _extract_watch_kvs
from jiuwenswarm.extensions.agentos.config_updater.client import FetchResult
from jiuwenswarm.extensions.agentos.config_updater.service import ConfigUpdaterApplier, ConfigUpdaterService


class ConfigUpdaterTest(unittest.IsolatedAsyncioTestCase):
    async def test_partial_update_preserves_local_settings_and_remote_overrides(self):
        baseline = {"agent_sandbox": {"idle_timeout": 60},
                    "gateway": {"agentos": {"auth_enabled": True}},
                    "sandbox": {"cpu": 2000, "memory": 4096}, "model": {"api_key": "local"}}
        original = copy.deepcopy(baseline)
        applied = []
        applier = ConfigUpdaterApplier(baseline, overrides_handler=applied.append)
        # Equal to the YAML baseline still needs to override a startup env value.
        result = await applier.apply(FetchResult(
            {"agent_sandbox": {"idle_timeout": 60},
             "gateway": {"agentos": {"auth_enabled": False}},
             "sandbox": {"cpu": 3000, "memory": 8192}, "model": {"api_key": "remote"}}, {}, 1))
        self.assertTrue(result.applied)
        self.assertEqual(applied[-1], {"agent_sandbox": {"idle_timeout": 60}})
        self.assertTrue((await applier.apply(FetchResult({"agent_sandbox": {"idle_timeout": 120}}, {}, 2))).applied)
        self.assertEqual(applied[-1], {"agent_sandbox": {"idle_timeout": 120}})
        self.assertEqual(baseline, original)
        self.assertEqual((await applier.apply(FetchResult({"agent_sandbox": {"idle_timeout": None}}, {}, 3))).skipped_reason,
                         "validation-failed")
        self.assertEqual((await applier.apply(FetchResult({"agent_sandbox": {"idle_timeout": 9000}}, {}, 2))).skipped_reason,
                         "revision-unchanged")
        self.assertEqual(len(applied), 2)

    async def test_failed_refresh_can_be_retried_without_advancing_revision(self):
        refresh = AsyncMock(side_effect=[False, True])
        applier = ConfigUpdaterApplier({}, refresh_handler=refresh)
        fetched = FetchResult({"agent_sandbox": {"idle_timeout": 900}}, {}, 12)
        self.assertTrue((await applier.apply(fetched)).retryable)
        self.assertEqual(applier.last_applied_mod_revision, 0)
        self.assertTrue((await applier.apply(fetched)).applied)
        self.assertEqual(applier.last_applied_mod_revision, 12)

    async def test_new_revision_supersedes_a_failed_revision_and_stop_closes_client(self):
        failed = asyncio.Event()
        completed = asyncio.Event()

        def apply(overrides):
            if overrides["agent_sandbox"]["idle_timeout"] == 1000:
                failed.set()
                raise RuntimeError("transient apply failure")
            completed.set()

        class Client:
            closed = False

            async def watch_loop(self, receive):
                await receive(FetchResult({"agent_sandbox": {"idle_timeout": 1000}}, {}, 1))
                await failed.wait()
                await receive(FetchResult({"agent_sandbox": {"idle_timeout": 2000}}, {}, 2))
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        client = Client()
        service = ConfigUpdaterService(etcd_endpoints=["http://etcd.test:2379"], config={},
                                       client=client, overrides_handler=apply)
        await service.start()
        try:
            await asyncio.wait_for(completed.wait(), 2)
        finally:
            await service.stop()
        self.assertTrue(client.closed)


class DeploymentAndSessionTest(unittest.TestCase):
    def test_deployment_defaults_are_scoped_and_explicit_local_config_wins(self):
        from jiuwenswarm.common import config

        deployment = {"type": "jiuwenbox-conch", "template_name": "agentos", "startup_mode": "internal"}
        env = {"JIUWENSWARM_AGENTOS_SANDBOX_CONFIG": json.dumps(deployment),
               "JIUWENSWARM_AGENTOS_CODE_GRAPH_CONFIG": '{"profile":"graph"}'}
        with patch.object(config, "_read_with_retry", return_value={"sandbox": {"type": "jiuwenbox"}}):
            with patch.dict("os.environ", {**env, "JIUWENSWARM_RUNTIME_PROFILE": ""}):
                self.assertNotIn("code_graph", config.get_config())
                self.assertNotIn("template_name", config.get_config()["sandbox"])
            with patch.dict("os.environ", {**env, "JIUWENSWARM_RUNTIME_PROFILE": "agentos"}):
                result = config.get_config()
                self.assertEqual(result["sandbox"]["type"], "jiuwenbox")
                self.assertEqual(result["sandbox"]["template_name"], "agentos")
                self.assertEqual(result["code_graph"]["profile"], "graph")

    def test_default_cloud_cwd_is_not_persisted_but_real_binding_is_retained(self):
        from jiuwenswarm.server.runtime.session import session_metadata as metadata

        writes = []
        params = dict(session_id="test", channel_id="web", project_dir="/home/agentos/workspace",
                      mode="team.code.normal", explicit_mode_provided=True, is_chat_turn=False)
        with patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": "agentos"}), \
             patch.object(metadata, "_read_metadata", return_value={}), \
             patch.object(metadata, "_enqueue_write", side_effect=lambda sid, value, **kw: writes.append(copy.deepcopy(value))):
            self.assertEqual(metadata.sync_session_request_metadata(**params), params["project_dir"])
        self.assertEqual(writes[-1]["project_dir"], "")
        self.assertEqual(writes[-1]["mode"], "team.code.normal")
        bound = {"session_id": "test", "project_id": "project1", "project_dir": "/projects/real",
                 "mode": "team.code.normal", "persist_session": True}
        with patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": "agentos"}), \
             patch.object(metadata, "_read_metadata", return_value=copy.deepcopy(bound)), \
             patch.object(metadata, "_enqueue_write", side_effect=lambda sid, value, **kw: writes.append(copy.deepcopy(value))):
            self.assertEqual(metadata.sync_session_request_metadata(**params), "/projects/real")
        self.assertEqual(writes[-1]["project_dir"], "/projects/real")
        with patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": ""}), \
             patch.object(metadata, "_read_metadata", return_value={}), \
             patch.object(metadata, "_enqueue_write", side_effect=lambda sid, value, **kw: writes.append(copy.deepcopy(value))):
            metadata.sync_session_request_metadata(**params)
        self.assertEqual(writes[-1]["project_dir"], params["project_dir"])

    def test_compacted_watch_forces_reconnect_instead_of_silently_missing_updates(self):
        with self.assertRaises(EtcdError):
            _extract_watch_kvs({"result": {"canceled": True, "compact_revision": "7"}})


class SandboxBoundaryTest(unittest.TestCase):
    def test_personal_yuanrong_preserves_mounts_rootfs_and_workdir(self):
        from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = {"source": str(root), "target": str(root), "readonly": False}
            custom = {"source": "/host/custom", "target": "/custom", "readonly": True}
            rootfs_mount = {"source": "/host/rootfs", "target": "/rootfs", "readonly": False}
            endpoint = {"executor": "docker", "mounts": [identity, custom, custom],
                        "rootfs": {"mounts": [rootfs_mount]}, "cpu": 2000, "memory": 4096,
                        "user": 1000, "group": 0}
            original = copy.deepcopy(endpoint)
            with patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": "",
                                            "JIUWENSWARM_USER_DIRECTORY": "/host/unused"}), \
                 patch.object(builder, "get_sandbox_endpoint", return_value=endpoint), \
                 patch.object(builder, "get_agent_root_dir", return_value=root):
                result = builder._build_yuanrong_extra_params()
                self.assertEqual(result["mounts"], [identity, custom])
                self.assertEqual(result["rootfs"]["mounts"], [identity, rootfs_mount])
                self.assertEqual(result["workdir"], str(root))
                self.assertEqual(result["rootfs"]["workdir"], str(root))
                self.assertEqual((result["cpu"], result["memory"]), (2000, 4096))
                self.assertNotIn("user", result)
                endpoint["workdir"] = "/chosen"
                endpoint["rootfs"]["workdir"] = "/rootfs-chosen"
                explicit = builder._build_yuanrong_extra_params()
                self.assertEqual(explicit["workdir"], "/chosen")
                self.assertEqual(explicit["rootfs"]["workdir"], "/rootfs-chosen")
            self.assertEqual(endpoint, {**original, "workdir": "/chosen",
                                        "rootfs": {**original["rootfs"], "workdir": "/rootfs-chosen"}})

    def test_agentos_yuanrong_keeps_user_mapping_and_zero_ids(self):
        from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            workspace = base / "agent" / "workspace"
            project = base / "workspace"
            workspace.mkdir(parents=True)
            project.mkdir()
            user_dir = base / "host-user"
            endpoint = {"executor": "docker", "user": 1000, "group": 0,
                        "mounts": [{"source": "/ignored", "target": "/ignored"}], "rootfs": {}}
            with patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": "agentos",
                                            "JIUWENSWARM_USER_DIRECTORY": str(user_dir)}), \
                 patch.object(builder, "get_sandbox_endpoint", return_value=endpoint), \
                 patch.object(builder, "_resolve_workspace_dir", return_value=workspace), \
                 patch.object(builder, "Path", side_effect=lambda value: project if value == "~/workspace" else Path(value)):
                result = builder._build_yuanrong_extra_params()
            self.assertEqual(result["mounts"], [
                {"source": str(user_dir / ".jiuwenswarm" / "agent" / "workspace"),
                 "target": str(workspace), "readonly": False},
                {"source": str(user_dir / "workspace"), "target": str(project), "readonly": False},
            ])
            self.assertEqual(result["rootfs"]["mounts"], result["mounts"])
            self.assertEqual(result["workdir"], str(workspace))
            self.assertEqual(result["user"], "1000:0")
            for user, group in ((0, 0), (1000, "0"), ("0", 1000)):
                with self.subTest(user=user, group=group):
                    self.assertEqual(builder._yuanrong_sandbox_user({"user": user, "group": group}),
                                     f"{user}:{group}")

    def test_status_fallback_and_data_root_skip_are_agentos_only(self):
        from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

        custom = {"source": "/host/custom", "target": "/custom", "readonly": True}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for profile, expected in (("", [custom]), ("agentos", [])):
                with self.subTest(profile=profile), \
                     patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": profile,
                                               "JIUSWARM_SANDBOX_PROJECT_DIR": ""}), \
                     patch.object(builder, "get_sandbox_endpoint", return_value={"mounts": [custom]}), \
                     patch.object(builder, "get_sandbox_runtime", return_value={"enabled": True}), \
                     patch.object(builder, "_build_yuanrong_extra_params", side_effect=ValueError("unavailable")), \
                     patch.object(builder, "get_user_workspace_dir", return_value=root), \
                     patch.object(Path, "cwd", return_value=root):
                    self.assertEqual(builder.build_yuanrong_sandbox_status_view()["mounts"], expected)
                    self.assertEqual(builder._resolve_project_dir(root), None if profile else root)
                    paths = builder.list_auto_managed_sandbox_paths(project_dir=root, startup_mode="external")
                    self.assertEqual(any(item["path"].rstrip("/") == str(root)
                                         for item in paths["allow_write"]), not bool(profile))

    def test_conch_preserves_numeric_zero_run_as_ids(self):
        from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

        for user, group in ((1000, 0), (0, 0)):
            with self.subTest(user=user, group=group), \
                 patch.object(builder, "get_sandbox_endpoint", return_value={
                     "template_name": "agentos", "user": user, "group": group}), \
                 patch.object(builder, "build_filesystem_policy", return_value=({"filesystem_policy": {}}, [])), \
                 patch.object(builder, "_sandbox_isolation_custom_id", return_value="test"):
                card = builder.create_sandbox_sysop_card("http://localhost:8321", "jiuwenbox-conch",
                                                         files_runtime={"allow": [], "deny": []})
            self.assertIsNotNone(card)
            extra = card.gateway_config.launcher_config.extra_params
            self.assertEqual(extra["api_sandbox_runtime"], "conch")
            self.assertEqual(extra["policy"]["conch"]["run_as_user"], str(user))
            self.assertEqual(extra["policy"]["conch"]["run_as_group"], str(group))


class SessionModeBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_handlers_keep_canonical_mode_with_legacy_wire_field(self):
        from jiuwenswarm.common.schema.agent import AgentRequest
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.server.control.repositories import session_repository
        from jiuwenswarm.server.runtime.gateway_adapter import session_adapter

        metadata = {"session_id": "test", "mode": "team.code.normal", "wire_mode": "team"}
        adapter = object.__new__(session_adapter.SessionAdapter)
        for profile in ("", "agentos"):
            for channel in ("web", "tui"):
                request = AgentRequest(request_id="test", channel_id=channel,
                                       req_method=ReqMethod.SESSION_GET_METADATA,
                                       params={"session_id": "test"})
                with self.subTest(profile=profile, channel=channel), \
                     patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": profile}), \
                     patch.object(session_repository, "get_session_metadata", return_value=metadata), \
                     patch.object(session_adapter, "get_session_metadata", return_value=metadata):
                    for response in (await session_repository.handle_get_metadata(request),
                                     await adapter._handle_get_metadata(request)):
                        self.assertTrue(response.ok)
                        self.assertEqual(response.payload["mode"], "team.code.normal")
        self.assertEqual(metadata["wire_mode"], "team")


class CodeGraphTest(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

        adapter = object.__new__(JiuwenSwarmCodeAdapter)
        adapter._code_graph_sdk_available = None
        adapter._code_graph_profile_rail = None
        adapter._code_graph_warmup_task = None
        adapter._code_graph_settings = None
        adapter._agent_workspace_dir = "/users/test"
        adapter._project_dir = "/projects/test"
        return adapter

    async def test_personal_runtime_does_not_initialize_graph_even_if_enabled(self):
        adapter = self.adapter()
        with patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": ""}):
            await adapter._sync_code_graph({"code_graph": {"profile": "graph"}})
            self.assertEqual(adapter._code_graph_flags({"code_graph": {"profile": "graph"}}).profile, "off")
        self.assertIsNone(adapter._code_graph_sdk_available)

    async def test_missing_sdk_keeps_text_search_and_status_reports_unavailable(self):
        from jiuwenswarm.server.agent_ws_server import resolve_status_code_graph

        adapter = self.adapter()
        config = {"code_graph": {"profile": "graph", "agent": "root"}}
        with patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": "agentos"}), \
             patch.dict("sys.modules", {"openjiuwen.core.retrieval.code_graph.models": None,
                                        "openjiuwen.core.retrieval.code_graph.manager": None}):
            await adapter._sync_code_graph(config)
            self.assertEqual(adapter._code_agent_graph_kwargs(config), {})
            self.assertEqual(resolve_status_code_graph(config, "/projects/test")["state"], "unavailable")
            self.assertEqual(resolve_status_code_graph({}, "/projects/test")["state"], "absent")
        self.assertFalse(adapter._code_graph_sdk_available)

    def test_cache_follows_existing_user_workspace_without_extra_hash_namespace(self):
        from pathlib import Path

        adapter = self.adapter()
        cache = adapter._resolve_code_graph_cache_dir(None)
        self.assertEqual(cache, str((Path(adapter._agent_workspace_dir) / ".code_graph_cache").resolve()))
        adapter._agent_workspace_dir = "/users/other"
        self.assertNotEqual(cache, adapter._resolve_code_graph_cache_dir(None))

    async def test_sdk_rail_survives_reload_and_warmup_tracks_config_changes(self):
        """Exercise the enabled path without installing a speculative SDK build."""
        class Rail:
            def __init__(self, profile, config=None, retrieval_interface=None):
                self.profile = profile
                self.abandon_graph = Mock()

        adapter = self.adapter()
        adapter._code_graph_sdk_available = True
        adapter._workspace_dir = "/projects/test"
        adapter._code_graph_settings = None
        adapter._code_graph_warmup_task = None
        adapter._instance = SimpleNamespace(deep_config=SimpleNamespace(), ensure_initialized=AsyncMock(),
                                            find_rails_by_type=lambda types: [], register_rail=AsyncMock())
        manager = SimpleNamespace(stats=lambda *a, **kw: {"state": "unavailable"})
        modules = {
            "openjiuwen.core.retrieval.code_graph.manager": SimpleNamespace(get_code_graph_manager=lambda cfg: manager),
            "openjiuwen.harness.rails.code_graph_profile_rail": SimpleNamespace(CodeGraphProfileRail=Rail),
        }
        cfg = SimpleNamespace(cache_dir="/users/test/.code_graph_cache")
        graph = {"code_graph": {"profile": "graph", "agent": "root"}}
        with patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": "agentos"}), \
             patch.dict("sys.modules", modules), \
             patch.object(adapter, "_build_code_graph_config", return_value=cfg), \
             patch.object(adapter, "_warm_code_graph", AsyncMock()) as warm:
            await adapter._sync_code_graph(graph)
            await asyncio.sleep(0)
            first_rail = adapter._code_graph_profile_rail
            await adapter._sync_code_graph(graph)
            self.assertIs(adapter._code_graph_profile_rail, first_rail)
            self.assertEqual(first_rail.abandon_graph.call_count, 2)
            self.assertEqual(warm.await_count, 1)
            await adapter._sync_code_graph({"code_graph": {"profile": "off"}})
            self.assertIsNone(adapter._code_graph_profile_rail)
            await adapter._sync_code_graph(graph)
            await asyncio.sleep(0)
            self.assertEqual(warm.await_count, 2)
            self.assertEqual(adapter._instance.deep_config.code_graph_config, cfg)
            # code_agent owns its own rail; disabling must still cancel the
            # parent's preloader and allow the next enable to rebuild.
            child_graph = {"code_graph": {"profile": "graph", "agent": "code_agent"}}
            await adapter._sync_code_graph(child_graph)
            await asyncio.sleep(0)
            self.assertIsNone(adapter._code_graph_profile_rail)
            await adapter._sync_code_graph({"code_graph": {"profile": "off"}})
            self.assertIsNone(adapter._code_graph_warmup_task)
            await adapter._sync_code_graph(child_graph)
            await asyncio.sleep(0)
            self.assertEqual(warm.await_count, 4)


if __name__ == "__main__":
    unittest.main()
