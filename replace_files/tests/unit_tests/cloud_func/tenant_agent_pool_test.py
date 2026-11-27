import asyncio
import json
from dataclasses import dataclass
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from jiuwenclaw.schema.agent import AgentRequest
from jiuwenclaw.schema.message import ReqMethod
from jiuwenclaw.agentserver.tenant_agent_pool import TenantAgentPool


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
    chunk_count = 0
    async for chunk in handler.process_message_stream(request):
        chunk_count += 1


async def run_non_stream_request(handler, request: AgentRequest) -> None:
    """执行非流式请求并打印结果."""
    result = await handler.process_message(request)


class TestTenantAgentPool(TestCase):
    """TenantAgentPool 单元测试"""

    def setUp(self) -> None:
        """每个测试前重置单例."""
        TenantAgentPool.reset_instance()

    @patch("jiuwenclaw.gateway.cron.CronController")
    @patch("jiuwenclaw.agentserver.interface.JiuWenClaw")
    def test_jiuwenclaw(self, mock_jiuwenclaw, mock_cron_controller) -> None:
        """对应原 test_jiuwenclaw：单用户流式请求（plan + agent 模式）."""

        mock_agent_instance = MagicMock()
        mock_agent_instance.create_instance = AsyncMock()

        async def _fake_process_message_stream(request):
            return
            yield

        mock_agent_instance.process_message_stream = _fake_process_message_stream
        mock_jiuwenclaw.return_value = mock_agent_instance

        mock_cron_controller.get_instance.return_value = MagicMock()

        async def _run():
            handler = TenantAgentPool.get_instance()

            # hello_stream: mode="plan"
            req1 = build_request(RequestParams(
                request_id="req_mn3zzbar_9",
                session_id="sess_19d1a822ed6_228b32",
                query="你好",
                is_stream=True,
                agent_id="default_agent",
                service_id="default_service",
                mode="plan",
            ))
            await run_stream_request(handler, req1)

            # plus_one_stream: mode="agent"
            req2 = build_request(RequestParams(
                request_id="req_mn3zzyec_14",
                session_id="sess_19d1a822ed6_228b32",
                query="1+1等于几",
                is_stream=True,
                agent_id="default_agent",
                service_id="default_service",
                mode="agent",
            ))
            await run_stream_request(handler, req2)

        asyncio.run(_run())

    @patch("jiuwenclaw.gateway.cron.CronController")
    @patch("jiuwenclaw.agentserver.interface.JiuWenClaw")
    def test_jiuwenclaw_not_stream(self, mock_jiuwenclaw, mock_cron_controller) -> None:
        """对应原 test_jiuwenclaw_not_stream：单用户非流式请求."""

        mock_agent_instance = MagicMock()
        mock_agent_instance.create_instance = AsyncMock()
        mock_agent_instance.process_message = AsyncMock(return_value=MagicMock())
        mock_jiuwenclaw.return_value = mock_agent_instance

        mock_cron_controller.get_instance.return_value = MagicMock()

        async def _run():
            handler = TenantAgentPool.get_instance()

            # hello_not_stream: mode="plan"
            req1 = build_request(RequestParams(
                request_id="req_mn3zzbar_9",
                session_id="sess_19d1a822ed6_228b32",
                query="你好",
                is_stream=False,
                agent_id="default_agent",
                service_id="default_service",
                mode="plan",
            ))
            await run_non_stream_request(handler, req1)

            # plus_one_not_stream: mode="agent"
            req2 = build_request(RequestParams(
                request_id="req_mn3zzyec_14",
                session_id="sess_19d1a822ed6_228b32",
                query="1+1等于几",
                is_stream=False,
                agent_id="default_agent",
                service_id="default_service",
                mode="agent",
            ))
            await run_non_stream_request(handler, req2)

        asyncio.run(_run())


    @patch("jiuwenclaw.gateway.cron.CronController")
    @patch("jiuwenclaw.agentserver.interface.JiuWenClaw")
    def test_jiuwenclaw_multi_user(
            self,
            mock_jiuwenclaw,
            mock_cron_controller,
    ) -> None:
        """对应原 test_jiuwenclaw_multi_user：多用户隔离测试."""

        mock_agent_instance = MagicMock()
        mock_agent_instance.create_instance = AsyncMock()
        mock_agent_instance.process_message = AsyncMock(return_value=MagicMock())
        mock_jiuwenclaw.return_value = mock_agent_instance

        mock_cron_controller.get_instance.return_value = MagicMock()

        async def _run():
            handler = TenantAgentPool.get_instance()

            # multi_user: agent_id="_userID", instance_id="_groupID_botID"
            req1 = build_request(RequestParams(
                request_id="req_mn3zzbar_9",
                session_id="sess_19d1a822ed6_228b32",
                query="你好",
                is_stream=False,
                agent_id="_userID_agent",
                service_id="_userID_service",
                mode="plan",
            ))
            await run_non_stream_request(handler, req1)

            req2 = build_request(RequestParams(
                request_id="req_mn3zzyec_14",
                session_id="sess_19d1a822ed6_228b32",
                query="1+1等于几",
                is_stream=False,
                agent_id="_userID_agent",
                service_id="_userID_service",
                mode="agent",
            ))
            await run_non_stream_request(handler, req2)

            # multi_user_02: agent_id="_userID_02"
            req3 = build_request(RequestParams(
                request_id="req_mn3zzbar_9",
                session_id="sess_19d1a822ed6_228b32",
                query="你好",
                is_stream=False,
                agent_id="_userID_02_agent",
                service_id="_userID_02_service",
                mode="plan",
            ))
            await run_non_stream_request(handler, req3)

            req4 = build_request(RequestParams(
                request_id="req_mn3zzyec_14",
                session_id="sess_19d1a822ed6_228b32",
                query="1+1等于几",
                is_stream=False,
                agent_id="_userID_02_agent",
                service_id="_userID_02_service",
                mode="agent",
            ))
            await run_non_stream_request(handler, req4)

            # multi_user_03: agent_id="_userID_01", instance_id="_groupID_botID_02"
            req5 = build_request(RequestParams(
                request_id="req_mn3zzbar_9",
                session_id="sess_19d1a822ed6_228b32",
                query="你好",
                is_stream=False,
                agent_id="_userID_01_agent",
                service_id="_userID_01_service",
                mode="plan",
            ))
            await run_non_stream_request(handler, req5)

            req6 = build_request(RequestParams(
                request_id="req_mn3zzyec_14",
                session_id="sess_19d1a822ed6_228b32",
                query="1+1等于几",
                is_stream=False,
                agent_id="_userID_01_agent",
                service_id="_userID_01_service",
                mode="agent",
            ))
            await run_non_stream_request(handler, req6)

            # multi_user_04: agent_id="_userID_01", instance_id="_groupID_botID_02" (同 user_03)
            req7 = build_request(RequestParams(
                request_id="req_mn3zzbar_9",
                session_id="sess_19d1a822ed6_228b32",
                query="你好",
                is_stream=False,
                agent_id="_userID_01_agent",
                service_id="_userID_01_service",
                mode="plan",
            ))
            await run_non_stream_request(handler, req7)

            req8 = build_request(RequestParams(
                request_id="req_mn3zzyec_14",
                session_id="sess_19d1a822ed6_228b32",
                query="1+1等于几",
                is_stream=False,
                agent_id="_userID_01_agent",
                service_id="_userID_01_service",
                mode="agent",
            ))
            await run_non_stream_request(handler, req8)

            # 验证实例数量（应该有 3 个不同的 agent_id）
            agent_count = await handler.get_agent_count()
            self.assertGreaterEqual(agent_count, 3, "应至少有 3 个不同 agent 的实例")

        asyncio.run(_run())

    def test_instance_id_building(self) -> None:
        """instance_id 构建逻辑测试."""
        handler = TenantAgentPool.get_instance()

        # 正常情况
        instance_id = handler.build_service_id("chat123", "bot456")
        self.assertEqual(instance_id, "chat123_bot456")

        # chat_id 为空
        instance_id_empty_chat = handler.build_service_id(None, "bot456")
        self.assertEqual(instance_id_empty_chat, "unknown_chat_id_bot456")

        # bot_app_id 为空
        instance_id_empty_bot = handler.build_service_id("chat123", None)
        self.assertEqual(instance_id_empty_bot, "chat123_unknown_bot_app_id")

        # 都为空
        instance_id_both_empty = handler.build_service_id(None, None)
        self.assertEqual(instance_id_both_empty, "unknown_chat_id_unknown_bot_app_id")

    def test_get_lock_recreates_on_different_event_loop(self) -> None:
        """测试 _get_lock 在事件循环变化时能正确重建锁."""

        async def first_loop():
            """在第一个事件循环中创建并使用锁."""
            pool = TenantAgentPool.get_instance()
            lock1 = pool._get_lock("test_key")
            self.assertIsInstance(lock1, asyncio.Lock)
            # 触发锁的使用
            async with lock1:
                pass
            return True

        # 第一次调用
        result1 = asyncio.run(first_loop())
        self.assertTrue(result1)

        # 注意：这里不调用 reset_instance()，让单例跨越事件循环
        async def second_loop():
            """在第二个事件循环中获取锁，应该能正常工作."""
            current_loop_id = id(asyncio.get_running_loop())

            pool = TenantAgentPool.get_instance()
            lock2 = pool._get_lock("test_key")
            self.assertIsInstance(lock2, asyncio.Lock)
            # 验证锁可以在新的事件循环中正常使用
            async with lock2:
                pass
            return True

        result2 = asyncio.run(second_loop())
        self.assertTrue(result2)

    def test_get_lock_returns_same_lock_in_same_loop(self) -> None:
        """测试在同一事件循环中多次调用 _get_lock 返回同一个锁对象."""

        async def _run():
            pool = TenantAgentPool.get_instance()
            lock1 = pool._get_lock("same_loop_key")
            lock2 = pool._get_lock("same_loop_key")
            self.assertIs(
                lock1,
                lock2,
                "同一事件循环中应返回相同的锁对象",
            )

        asyncio.run(_run())

    def test_get_lock_creates_new_for_different_keys(self) -> None:
        """测试不同 cache_key 会创建不同的锁对象."""

        async def _run():
            pool = TenantAgentPool.get_instance()
            lock1 = pool._get_lock("key_a")
            lock2 = pool._get_lock("key_b")
            self.assertIsNot(
                lock1,
                lock2,
                "不同 cache_key 应该有不同的锁对象",
            )

        asyncio.run(_run())

    @patch("jiuwenclaw.agentserver.interface.JiuWenClaw")
    def test_ensure_agent_manager_across_event_loops(
            self,
            mock_jiuwenclaw,
    ) -> None:
        """测试跨事件循环调用 _ensure_agent_manager 不会报错."""
        mock_agent_instance = MagicMock()
        mock_agent_instance.create_instance = AsyncMock()
        mock_agent_instance.process_message = AsyncMock(return_value=MagicMock())
        mock_jiuwenclaw.return_value = mock_agent_instance

        async def first_request():
            """第一次请求（第一个事件循环）."""
            pool = TenantAgentPool.get_instance()
            manager = await pool._ensure_agent_manager("test_agent", "test_service")
            self.assertIsNotNone(manager)
            return True

        async def second_request():
            """第二次请求（新的事件循环），应该能正常获取锁."""
            pool = TenantAgentPool.get_instance()
            manager = await pool._ensure_agent_manager("test_agent", "test_service")
            self.assertIsNotNone(manager)
            return True

        result1 = asyncio.run(first_request())
        result2 = asyncio.run(second_request())

        self.assertTrue(result1)
        self.assertTrue(result2)

    @patch("jiuwenclaw.agentserver.interface.JiuWenClaw")
    def test_concurrent_requests_after_event_loop_change(
            self,
            mock_jiuwenclaw,
    ) -> None:
        """测试事件循环变化后，并发请求能正常工作."""

        mock_agent_instance = MagicMock()
        mock_agent_instance.create_instance = AsyncMock()
        mock_agent_instance.process_message = AsyncMock(return_value=MagicMock())
        mock_jiuwenclaw.return_value = mock_agent_instance

        async def simulate_multiple_requests():
            """模拟多个并发请求."""
            pool = TenantAgentPool.get_instance()

            tasks = []
            for i in range(5):
                task = pool._ensure_agent_manager(f"agent_{i}", f"service_{i}")
                tasks.append(task)

            results = await asyncio.gather(*tasks)
            self.assertEqual(len(results), 5)
            for result in results:
                self.assertIsNotNone(result)

        asyncio.run(simulate_multiple_requests())

    def test_get_lock_detects_loop_change(self) -> None:
        """验证 _get_lock 能检测到事件循环变化并重建锁."""

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
            lock2_id = id(lock2)
            # 关键断言：必须是不同的锁对象
            self.assertNotEqual(lock1_id, lock2_id,
                                "事件循环变化后应创建新锁")
            async with lock2:
                pass

        asyncio.run(first_loop())
        asyncio.run(second_loop())

    def test_lock_with_waiters_across_event_loop(self):
        """测试锁在有等待者时跨事件循环的场景"""

        async def first_loop_hold_lock():
            pool = TenantAgentPool.get_instance()
            lock = pool._get_lock("test_key")
            await lock.acquire()  # 获取锁但不释放
            return lock

        lock1 = asyncio.run(first_loop_hold_lock())

        async def second_loop_use_lock():
            pool = TenantAgentPool.get_instance()
            # 此时 pool._locks["test_key"] 仍然是 lock1，但 lock1 绑定到已销毁的事件循环
            lock2 = pool._get_lock("test_key")
            # 如果 _get_lock 正确重建，lock2 应该是新锁，否则会出现 RuntimeError
            async with lock2:
                pass

        asyncio.run(second_loop_use_lock())

    @patch("jiuwenclaw.agentserver.interface.JiuWenClaw")
    def test_concurrent_waiters_across_event_loop(self, mock_jiuwenclaw):
        """测试有等待者的锁在事件循环变化时的行为."""

        mock_agent_instance = MagicMock()
        mock_agent_instance.create_instance = AsyncMock()
        mock_jiuwenclaw.return_value = mock_agent_instance

        async def first_loop_with_concurrent_requests():
            pool = TenantAgentPool.get_instance()
            # 启动多个并发请求，让它们竞争同一把锁
            tasks = []
            for i in range(3):
                task = asyncio.create_task(
                    pool._ensure_agent_manager("shared_agent", "shared_service")
                )
                tasks.append(task)
            # 等待一小段时间，确保有些任务在等待锁
            await asyncio.sleep(0.01)
            # 此时可能有任务在等待锁（waiters > 0），不等待完成，直接返回（模拟事件循环中断）
            return tasks

        # 第一个事件循环
        tasks = asyncio.run(first_loop_with_concurrent_requests())
        # 清理未完成的任务
        for task in tasks:
            task.cancel()
        # 第二个事件循环 - 应该能正常处理新请求
        async def second_loop():
            pool = TenantAgentPool.get_instance()
            manager = await pool._ensure_agent_manager("shared_agent", "shared_service")
            self.assertIsNotNone(manager)

        asyncio.run(second_loop())
