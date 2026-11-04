"""Policy persistence/selection with injected runtimes; no bwrap/Conch daemon."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

# Resolve the source package rather than the repository's namespace directory
# when running unittest directly without installing the JiuwenBox subproject.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jiuwenbox" / "src"))

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.models.sandbox import ExecResult, PolicyMode, SandboxPhase, SandboxRef, SandboxSpec
from jiuwenbox.server.etcd_sync.client import FetchResult
from jiuwenbox.server.etcd_sync.service import PolicySyncService
from jiuwenbox.server.runtime.errors import BackgroundJobNotFoundError, PolicyValidationError


def _load_manager(testcase):
    # Production JiuwenBox runs on Linux. Inject only its OS-dependent imports
    # on Windows so the same orchestration can be tested with fake runtimes.
    if sys.platform != "win32":
        from jiuwenbox.server.sandbox_manager import SandboxManager
        return SandboxManager
    workspace = ModuleType("jiuwenbox.server.workspace")
    workspace.JIUWENBOX_HOME = Path(tempfile.gettempdir()) / "agentos-policy-unit"
    workspace.SANDBOX_WORKSPACE = workspace.JIUWENBOX_HOME / "workspace"
    process = ModuleType("jiuwenbox.server.runtime.process")
    process.ProcessRuntime = Mock
    process.BackgroundJobNotFoundError = BackgroundJobNotFoundError
    imports = patch.dict(sys.modules, {workspace.__name__: workspace, process.__name__: process})
    imports.start()
    testcase.addCleanup(imports.stop)
    from jiuwenbox.server.sandbox_manager import SandboxManager
    return SandboxManager


class AgentOSPolicyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.Manager = _load_manager(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def manager(self, *, agentos):
        from jiuwenbox.server.policy_engine import PolicyEngine

        with patch.dict(os.environ, {"JIUWENBOX_RUNTIME_PROFILE": "agentos" if agentos else ""}):
            return self.Manager(runtime=Mock(), policy_engine=PolicyEngine(self.base / "policies"),
                                policy_reader=SimpleNamespace(load_policy=lambda: SecurityPolicy()),
                                state_dir=self.base / "state",
                                update_policy_path=self.base / "update.yaml" if agentos else None)

    async def test_default_policy_snapshot_survives_restart_and_partial_updates(self):
        manager = self.manager(agentos=True)
        result = await manager.update_all_policies(
            {"conch": {"template_name": "agentos-template", "ram_mb": 1024}},
            update_default_policy=True, update_existing_sandboxes=False)
        self.assertTrue(result["default_updated"])
        await manager.update_all_policies({"conch": {"ram_mb": 2048}},
                                         update_default_policy=True, update_existing_sandboxes=False)
        restored = self.manager(agentos=True)
        self.assertEqual(restored.policy.conch.template_name, "agentos-template")
        self.assertEqual(restored.policy.conch.ram_mb, 2048)
        self.assertEqual(restored.policy.network, SecurityPolicy().network)
        self.assertFalse((self.base / "update.yaml.tmp").exists())

    async def test_personal_mode_keeps_process_runtime_and_rejects_default_persistence(self):
        manager = self.manager(agentos=False)
        self.assertIs(manager._runtime_for(SandboxRef(id="test")), manager.runtime)
        self.assertIsNone(manager._conch_runtime)
        with self.assertRaises(PolicyValidationError):
            await manager.update_all_policies({"name": "remote"}, update_default_policy=True)
        manager._update_all_network_policies = AsyncMock(return_value={"updated": [], "skipped": [], "failed": []})
        await manager.update_all_policies({"network": {}})
        manager._update_all_network_policies.assert_awaited_once()
        self.assertFalse((self.base / "update.yaml").exists())
        current = SecurityPolicy.model_validate({"network": {"egress": {"default": "allow", "allowed_ips": ["1.2.3.4"]}}})
        replaced = manager._merge_network_rules_update(current, {"allowed_ports": [443]}, None, PolicyMode.OVERRIDE)
        self.assertEqual(replaced.network.egress.default, "deny")
        self.assertEqual(replaced.network.egress.allowed_ips, [])

    async def test_concurrent_default_fragments_preserve_both_updates(self):
        manager = self.manager(agentos=True)
        async with manager._lock:
            tasks = [asyncio.create_task(manager.update_all_policies(
                fragment, update_default_policy=True, update_existing_sandboxes=False,
            )) for fragment in ({"conch": {"template_name": "agentos-template"}}, {"conch": {"ram_mb": 4096}})]
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        restored = self.manager(agentos=True)
        self.assertEqual(restored.policy.conch.template_name, "agentos-template")
        self.assertEqual(restored.policy.conch.ram_mb, 4096)

    async def test_conch_create_exec_delete_dispatches_and_preserves_ref_wire_field(self):
        from jiuwenbox.server.sandbox_manager import SandboxExecRequest

        manager = self.manager(agentos=True)
        manager._conch_runtime = SimpleNamespace(create=AsyncMock(return_value=None),
                                                 exec=AsyncMock(return_value=ExecResult(exit_code=0, stdout="ok", stderr="")),
                                                 cleanup=AsyncMock())
        manager._resolve_sandbox_ip_address = AsyncMock(return_value=None)
        ref = await manager.create_sandbox(SandboxSpec(sandbox_runtime="conch"),
                                          policy_data={"conch": {"template_name": "agentos-template"}})
        self.assertEqual(ref.phase, SandboxPhase.READY)
        self.assertEqual(ref.model_dump()["runtime"], "conch")
        self.assertNotIn("sandbox_runtime", ref.model_dump())
        result = await manager.exec_in_sandbox(ref.id, SandboxExecRequest(command=["echo", "ok"]))
        self.assertEqual(result.stdout, "ok")
        await manager.delete_sandbox(ref.id)
        manager._conch_runtime.create.assert_awaited_once()
        manager._conch_runtime.exec.assert_awaited_once()
        manager._conch_runtime.cleanup.assert_awaited_once_with(ref.id)
        manager.runtime.create.assert_not_called()
        self.assertNotIn(ref.id, manager._sandboxes)

    async def test_policy_sync_does_not_ack_failed_live_updates_and_is_agentos_only(self):
        manager = SimpleNamespace(update_all_policies=AsyncMock(side_effect=[
            {"failed": [{"sandbox_id": "test"}]}, {"failed": [], "default_updated": True},
        ]))
        service = PolicySyncService(manager, etcd_endpoints=["http://etcd.test:2379"])
        fetched = FetchResult(section={"conch": {"ram_mb": 1024}}, metadata={}, mod_revision=7)
        await service.apply(fetched)
        self.assertEqual(service.last_applied_mod_revision, 0)
        await service.apply(fetched)
        self.assertEqual(service.last_applied_mod_revision, 7)
        await service.apply(fetched)
        self.assertEqual(manager.update_all_policies.await_count, 2)
        with patch.dict(os.environ, {"JIUWENBOX_ETCD_ENDPOINTS": "http://etcd.test:2379",
                                    "JIUWENBOX_RUNTIME_PROFILE": ""}):
            self.assertFalse(PolicySyncService.from_env(manager).enabled)

    async def test_new_policy_revision_supersedes_failure_and_closes_client(self):
        failed = asyncio.Event()
        done = asyncio.Event()

        async def update(**kwargs):
            if kwargs["policy_data"]["conch"]["ram_mb"] == 1024:
                failed.set()
                raise RuntimeError("transient runtime failure")
            done.set()
            return {"failed": []}

        class Client:
            closed = False

            async def watch_loop(self, receive):
                await receive(FetchResult({"conch": {"ram_mb": 1024}}, {}, 1))
                await failed.wait()
                await receive(FetchResult({"conch": {"ram_mb": 2048}}, {}, 2))
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        client = Client()
        service = PolicySyncService(SimpleNamespace(update_all_policies=update),
                                    etcd_endpoints=["http://etcd.test:2379"], client=client)
        await service.start()
        try:
            await asyncio.wait_for(done.wait(), 2)
        finally:
            await service.stop()
        self.assertTrue(client.closed)
        self.assertEqual(service.last_applied_mod_revision, 2)


if __name__ == "__main__":
    unittest.main()
