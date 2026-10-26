import asyncio
from dataclasses import dataclass
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.common.tools.deepresearch_task_manager import (
    DeepResearchTaskManagerPool,
)
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool


@dataclass
class RequestParams:
    """测试请求参数封装."""
    request_id: str
    session_id: str
    query: str
    is_stream: bool = False
    agent_id: str | None = None
    service_id: str | None = None
    mode: str = "agent"


def build_request(params: RequestParams) -> AgentRequest:
    """构建测试用的 AgentRequest."""
    return AgentRequest(
        request_id=params.request_id,
        channel_id="web",
        session_id=params.session_id,
        agent_id=params.agent_id,
        service_id=params.service_id,
        req_method=ReqMethod.CHAT_SEND,
        params={
            "session_id": params.session_id,
            "content": params.query,
            "mode": params.mode,
            "query": params.query,
        },
        is_stream=params.is_stream,
        timestamp=1774319436.5314593,
        metadata={"method": "chat.send"},
    )


async def run_stream_request(handler, request: AgentRequest) -> None:
    """执行流式请求并打印结果."""
    async for _chunk in handler.process_message_stream(request):
        pass


async def run_non_stream_request(handler, request: AgentRequest) -> None:
    """执行非流式请求并打印结果."""
    await handler.process_message(request)


class TestTenantAgentPool(TestCase):
    """TenantAgentPool 单元测试"""

    def setUp(self) -> None:
        """每个测试前重置单例."""
        TenantAgentPool.reset_instance()
        DeepResearchTaskManagerPool.reset_for_tests()

    def tearDown(self) -> None:
        TenantAgentPool.reset_instance()
        DeepResearchTaskManagerPool.reset_for_tests()

    def test_community_singleton(self) -> None:
        """非企业版时，同租户键复用同一 AgentManager."""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "personal"}, clear=False):
            TenantAgentPool.reset_instance()
            pool = TenantAgentPool.get_instance()
            mock_mgr = MagicMock()

            async def _run():
                with patch(
                    "jiuwenswarm.server.runtime.agent_manager.AgentManager",
                    return_value=mock_mgr,
                ):
                    mgr1 = await pool.get_agent_manager("default", "default")
                    mgr2 = await pool.get_agent_manager("default", "default")
                self.assertIs(mgr1, mock_mgr)
                self.assertIs(mgr1, mgr2)

            asyncio.run(_run())

    def test_enterprise_pool(self) -> None:
        """企业版下按 (agent_id, service_id) 隔离多租户 LRU."""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "enterprise"}, clear=False):
            TenantAgentPool.reset_instance()
            pool = TenantAgentPool.get_instance()

            def _factory(**kwargs):
                mgr = MagicMock()
                mgr.agent_id = kwargs.get("agent_id")
                mgr.service_id = kwargs.get("service_id")
                mgr.env_agent_id = kwargs.get("env_agent_id")
                mgr.env_service_id = kwargs.get("env_service_id")
                mgr._user_workspace_dir = kwargs.get("user_workspace_dir")
                return mgr

            async def _run():
                with patch(
                    "jiuwenswarm.server.runtime.agent_manager.AgentManager",
                    side_effect=_factory,
                ):
                    mgr1 = await pool.get_agent_manager("a1", "s1")
                    mgr2 = await pool.get_agent_manager("a1", "s1")
                    mgr3 = await pool.get_agent_manager("a2", "s2")
                self.assertIs(mgr1, mgr2)
                self.assertIsNot(mgr1, mgr3)
                # 企业版下 manager.agent_id 使用 legacy "aid_sid" 形态
                self.assertEqual(mgr1.agent_id, "a1_s1")
                self.assertEqual(mgr1.service_id, "s1")
                self.assertEqual(mgr1.env_agent_id, "a1")
                self.assertEqual(mgr1.env_service_id, "s1")
                self.assertIsNotNone(mgr1._user_workspace_dir)

            asyncio.run(_run())

    def test_build_service_id(self) -> None:
        self.assertEqual(
            TenantAgentPool.build_service_id("chat1", "bot1"),
            "chat1_bot1",
        )
        self.assertEqual(
            TenantAgentPool.build_service_id(None, None),
            "unknown_chat_id_unknown_bot_app_id",
        )

    def test_process_message_dispatch(self) -> None:
        """process_message 分发到对应租户的 AgentManager."""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "personal"}, clear=False):
            TenantAgentPool.reset_instance()
            pool = TenantAgentPool.get_instance()
            mock_resp = MagicMock()
            mock_mgr = MagicMock()
            mock_mgr.process_message = AsyncMock(return_value=mock_resp)

            async def _run():
                with patch.object(
                    pool,
                    "_ensure_agent_manager",
                    AsyncMock(return_value=mock_mgr),
                ) as ensure:
                    req = build_request(RequestParams(
                        request_id="r1",
                        session_id="s1",
                        query="hi",
                    ))
                    resp = await pool.process_message(req)
                self.assertIs(resp, mock_resp)
                ensure.assert_awaited()

            asyncio.run(_run())

    def test_evict_isolates_deepresearch_tenants_and_shuts_target_once(self) -> None:
        async def _run():
            pool = TenantAgentPool.get_instance()
            target = await DeepResearchTaskManagerPool.get_or_create(("svc-a", "agent-a"))
            other = await DeepResearchTaskManagerPool.get_or_create(("svc-b", "agent-b"))
            target.shutdown = AsyncMock()
            other.shutdown = AsyncMock()

            with (
                patch(
                    "jiuwenswarm.agents.harness.common.plugins.rail_manager.RailManagerPool.remove"
                ),
                patch(
                    "jiuwenswarm.server.runtime.cron_local_runtime.AgentCronRegistry.remove",
                    new=AsyncMock(),
                ),
            ):
                await pool._evict_manager_cache("agent-a", "svc-a")

            target.shutdown.assert_awaited_once_with()
            other.shutdown.assert_not_awaited()
            self.assertIs(
                await DeepResearchTaskManagerPool.get_or_create(("svc-b", "agent-b")),
                other,
            )
            self.assertIsNot(
                await DeepResearchTaskManagerPool.get_or_create(("svc-a", "agent-a")),
                target,
            )

        asyncio.run(_run())

    def test_evict_isolates_deepresearch_workspaces_and_shuts_target_once(self) -> None:
        async def _run():
            pool = TenantAgentPool.get_instance()
            target = await DeepResearchTaskManagerPool.get_or_create(
                ("svc-a", "agent-a", "workspace-a")
            )
            other = await DeepResearchTaskManagerPool.get_or_create(
                ("svc-a", "agent-a", "workspace-b")
            )
            target.shutdown = AsyncMock()
            other.shutdown = AsyncMock()

            with (
                patch(
                    "jiuwenswarm.agents.harness.common.plugins.rail_manager.RailManagerPool.remove"
                ),
                patch(
                    "jiuwenswarm.server.runtime.cron_local_runtime.AgentCronRegistry.remove",
                    new=AsyncMock(),
                ),
            ):
                await pool._evict_manager_cache(
                    "agent-a",
                    "svc-a",
                    "workspace-a",
                )

            target.shutdown.assert_awaited_once_with()
            other.shutdown.assert_not_awaited()
            self.assertIs(
                await DeepResearchTaskManagerPool.get_or_create(
                    ("svc-a", "agent-a", "workspace-b")
                ),
                other,
            )
            self.assertIsNot(
                await DeepResearchTaskManagerPool.get_or_create(
                    ("svc-a", "agent-a", "workspace-a")
                ),
                target,
            )

        asyncio.run(_run())


    def test_get_lock_recreates_on_different_event_loop(self) -> None:
        """_get_lock 在事件循环变化时能正确重建锁."""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "enterprise"}, clear=False):
            TenantAgentPool.reset_instance()

            async def first_loop():
                pool = TenantAgentPool.get_instance()
                lock1 = pool._get_lock("test_key")
                self.assertIsInstance(lock1, asyncio.Lock)
                async with lock1:
                    pass
                return True

            self.assertTrue(asyncio.run(first_loop()))

            async def second_loop():
                pool = TenantAgentPool.get_instance()
                lock2 = pool._get_lock("test_key")
                self.assertIsInstance(lock2, asyncio.Lock)
                async with lock2:
                    pass
                return True

            self.assertTrue(asyncio.run(second_loop()))

    def test_get_lock_returns_same_lock_in_same_loop(self) -> None:
        """同一事件循环中多次调用 _get_lock 返回同一个锁对象."""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "enterprise"}, clear=False):
            TenantAgentPool.reset_instance()

            async def _run():
                pool = TenantAgentPool.get_instance()
                lock1 = pool._get_lock("same_loop_key")
                lock2 = pool._get_lock("same_loop_key")
                self.assertIs(lock1, lock2)

            asyncio.run(_run())

    def test_get_lock_creates_new_for_different_keys(self) -> None:
        """不同 cache_key 会创建不同的锁对象."""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "enterprise"}, clear=False):
            TenantAgentPool.reset_instance()

            async def _run():
                pool = TenantAgentPool.get_instance()
                lock1 = pool._get_lock("key_a")
                lock2 = pool._get_lock("key_b")
                self.assertIsNot(lock1, lock2)

            asyncio.run(_run())

    def test_ensure_agent_manager_across_event_loops(self) -> None:
        """跨事件循环调用 _ensure_agent_manager 不会报错."""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "enterprise"}, clear=False):
            TenantAgentPool.reset_instance()

            async def first_request():
                pool = TenantAgentPool.get_instance()
                manager = await pool._ensure_agent_manager("test_agent", "test_service", "test_workspace")
                self.assertIsNotNone(manager)
                return True

            async def second_request():
                pool = TenantAgentPool.get_instance()
                manager = await pool._ensure_agent_manager("test_agent", "test_service", "test_workspace")
                self.assertIsNotNone(manager)
                return True

            with patch(
                "jiuwenswarm.server.runtime.agent_manager.AgentManager",
                return_value=MagicMock(),
            ):
                self.assertTrue(asyncio.run(first_request()))
                self.assertTrue(asyncio.run(second_request()))

    def test_concurrent_requests_after_event_loop_change(self) -> None:
        """事件循环变化后，并发请求能正常工作."""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "enterprise"}, clear=False):
            TenantAgentPool.reset_instance()

            async def simulate_multiple_requests():
                pool = TenantAgentPool.get_instance()
                tasks = [
                    pool._ensure_agent_manager(f"agent_{i}", f"service_{i}", f"workspace_{i}")
                    for i in range(5)
                ]
                results = await asyncio.gather(*tasks)
                self.assertEqual(len(results), 5)
                for result in results:
                    self.assertIsNotNone(result)

            with patch(
                "jiuwenswarm.server.runtime.agent_manager.AgentManager",
                return_value=MagicMock(),
            ):
                asyncio.run(simulate_multiple_requests())

    def test_get_lock_detects_loop_change(self) -> None:
        """验证 _get_lock 能检测到事件循环变化并重建锁."""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "enterprise"}, clear=False):
            TenantAgentPool.reset_instance()
            pool = TenantAgentPool.get_instance()
            lock1_id = None

            async def first_loop():
                nonlocal lock1_id
                lock1 = pool._get_lock("detect_key")
                lock1_id = id(lock1)
                async with lock1:
                    pass

            async def second_loop():
                lock2 = pool._get_lock("detect_key")
                self.assertNotEqual(lock1_id, id(lock2), "事件循环变化后应创建新锁")
                async with lock2:
                    pass

            asyncio.run(first_loop())
            asyncio.run(second_loop())

    def test_lock_with_waiters_across_event_loop(self) -> None:
        """锁在有占用时跨事件循环仍可重建后使用（不复用旧 loop 的锁）。"""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "enterprise"}, clear=False):
            TenantAgentPool.reset_instance()
            held_lock_id: int | None = None

            async def first_loop_hold_lock():
                nonlocal held_lock_id
                pool = TenantAgentPool.get_instance()
                lock = pool._get_lock("test_key")
                held_lock_id = id(lock)
                await lock.acquire()
                # 故意不 release：模拟旧 loop 上占用中的锁；新 loop 必须重建。
                return True

            self.assertTrue(asyncio.run(first_loop_hold_lock()))

            async def second_loop_use_lock():
                pool = TenantAgentPool.get_instance()
                lock2 = pool._get_lock("test_key")
                self.assertIsNotNone(held_lock_id)
                self.assertNotEqual(held_lock_id, id(lock2), "跨 loop 应重建锁")
                # 新锁必须立即可用，否则说明误复用了仍被占用的旧锁。
                async with asyncio.timeout(1):
                    async with lock2:
                        pass

            asyncio.run(second_loop_use_lock())

    def test_concurrent_waiters_across_event_loop(self) -> None:
        """有并发争用后，事件循环变化仍可服务新请求."""
        with patch.dict("os.environ", {"JIUWENSWARM_EDITION": "enterprise"}, clear=False):
            TenantAgentPool.reset_instance()

            async def first_loop_with_concurrent_requests():
                pool = TenantAgentPool.get_instance()
                results = await asyncio.gather(
                    *[
                        pool._ensure_agent_manager("shared_agent", "shared_service", "shared_workspace")
                        for _ in range(3)
                    ]
                )
                self.assertEqual(len(results), 3)
                for result in results:
                    self.assertIsNotNone(result)

            with patch(
                "jiuwenswarm.server.runtime.agent_manager.AgentManager",
                return_value=MagicMock(),
            ):
                asyncio.run(first_loop_with_concurrent_requests())

                async def second_loop():
                    pool = TenantAgentPool.get_instance()
                    manager = await pool._ensure_agent_manager(
                        "shared_agent", "shared_service", "shared_workspace"
                    )
                    self.assertIsNotNone(manager)

                asyncio.run(second_loop())
