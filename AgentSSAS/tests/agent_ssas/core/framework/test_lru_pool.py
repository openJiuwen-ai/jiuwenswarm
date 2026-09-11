# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""LRU 池多线程压力测试。

验证 IDManager 的 session 级 LRU 池在并发场景下的正确性:
- 并发自增无竞态
- LRU 淘汰策略正确
- 池大小不超过上限
- 不同 session 的序号互不干扰

此测试通过 sys.modules mock 绕过 openjiuwen 依赖,只测试 LRU 池内部逻辑。
使用模块级 fixture 在测试前设置 mock、测试后恢复,避免污染其他测试。
"""

from __future__ import annotations

import sys
import threading
import types
from unittest.mock import MagicMock

import pytest

# 需要被 mock 的 openjiuwen 模块列表(仅当真实模块未加载时才 mock)
_MOCK_TARGETS = [
    "openjiuwen",
    "openjiuwen.core",
    "openjiuwen.core.single_agent",
    "openjiuwen.core.single_agent.rail",
    "openjiuwen.core.single_agent.rail.base",
    "openjiuwen.core.common",
    "openjiuwen.core.common.logging",
    "openjiuwen.harness",
    "openjiuwen.harness.rails",
    "openjiuwen.harness.rails.security",
    "openjiuwen.harness.rails.security.base_security_rail",
]


@pytest.fixture(scope="module")
def id_manager_cls():
    """模块级 fixture:设置 sys.modules mock,导入 IDManager,测试后恢复。

    在 fixture 设置阶段临时 mock openjiuwen 模块(仅当真实模块未加载时),
    导入 IDManager 供测试使用,测试结束后删除所有 mock 模块和已加载的
    agent_ssas 模块,防止污染后续依赖真实 openjiuwen 的测试。
    """
    _mocked: dict[str, types.ModuleType] = {}

    for _mod_name in _MOCK_TARGETS:
        if _mod_name not in sys.modules:
            _mock = types.ModuleType(_mod_name)
            sys.modules[_mod_name] = _mock
            _mocked[_mod_name] = _mock

    # 仅为 mock 模块设置 mock 属性(不覆盖真实模块的类)
    if "openjiuwen.core.single_agent.rail.base" in _mocked:
        _mocked["openjiuwen.core.single_agent.rail.base"].AgentCallbackContext = MagicMock
        _mocked["openjiuwen.core.single_agent.rail.base"].AgentCallbackEvent = MagicMock

    if "openjiuwen.core.common.logging" in _mocked:
        _mocked["openjiuwen.core.common.logging"].logger = MagicMock()

    if "openjiuwen.harness.rails.security.base_security_rail" in _mocked:
        _ms = _mocked["openjiuwen.harness.rails.security.base_security_rail"]
        _ms.SecurityAlert = MagicMock
        _ms.SecurityAlertLevel = MagicMock
        _ms.SecurityAllow = MagicMock
        _ms.SecurityDecision = MagicMock
        _ms.SecurityReject = MagicMock
        _ms.SecurityInterrupt = MagicMock
        _ms.BaseSecurityRail = MagicMock
        _ms.SecurityCheckContext = MagicMock

    if "openjiuwen.harness.rails.security" in _mocked:
        _mocked["openjiuwen.harness.rails.security"].SecurityCheckContext = MagicMock

    # 删除可能已加载的 agent_ssas 模块,确保重新导入(用 mock 或真实模块)
    _agent_ssas_modules = [
        "agent_ssas.backend_client.openjiuwen.id_manager",
        "agent_ssas.backend_client.openjiuwen.agent_ssas_security_rail",
        "agent_ssas.backend_client.openjiuwen.event_reporter",
        "agent_ssas.backend_client.openjiuwen.event_builder",
        "agent_ssas.backend_client.openjiuwen.event_filter",
        "agent_ssas.backend_client.openjiuwen.extended_context",
    ]
    _saved_agent_ssas = {}
    for _mod in _agent_ssas_modules:
        if _mod in sys.modules:
            _saved_agent_ssas[_mod] = sys.modules.pop(_mod)

    # 导入 IDManager(此时 mock 已就位或真实模块已加载)
    from agent_ssas.backend_client.openjiuwen.id_manager import IDManager

    yield IDManager

    # 恢复:删除所有 mock 模块
    for _mod_name in _mocked:
        sys.modules.pop(_mod_name, None)
    # 删除用 mock 导入的 agent_ssas 模块,后续测试会重新导入真实版本
    for _mod in _agent_ssas_modules:
        sys.modules.pop(_mod, None)
    # 恢复之前保存的 agent_ssas 模块(如果有)
    for _mod, _orig in _saved_agent_ssas.items():
        if _mod not in sys.modules:
            sys.modules[_mod] = _orig


class TestLRUPoolConcurrency:
    """LRU 池并发压力测试。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_lru_pool_size_never_exceeds_max(id_manager_cls):
        """验证 LRU 池大小永远不超过 _MAX_SESSIONS。"""
        idm = id_manager_cls()
        original_max = id_manager_cls._MAX_SESSIONS
        id_manager_cls._MAX_SESSIONS = 10

        try:
            for i in range(50):
                idm._increment_interaction_seq_in_pool(f"session-{i:03d}")
            assert len(idm._session_interaction_seqs) <= 10
        finally:
            id_manager_cls._MAX_SESSIONS = original_max

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_lru_evicts_oldest_first(id_manager_cls):
        """验证 LRU 淘汰最久未使用的 session。"""
        idm = id_manager_cls()
        id_manager_cls._MAX_SESSIONS = 3

        try:
            # 插入 3 个 session
            for sid in ["s1", "s2", "s3"]:
                idm._increment_interaction_seq_in_pool(sid)
            assert len(idm._session_interaction_seqs) == 3

            # 访问 s1,使其成为最近使用
            idm._get_interaction_seq_from_pool("s1")

            # 插入 s4,应淘汰最久未使用的 s2
            idm._increment_interaction_seq_in_pool("s4")

            assert "s2" not in idm._session_interaction_seqs
            assert "s1" in idm._session_interaction_seqs
            assert "s3" in idm._session_interaction_seqs
            assert "s4" in idm._session_interaction_seqs
        finally:
            id_manager_cls._MAX_SESSIONS = 100

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_concurrent_increment_same_session_no_race(id_manager_cls):
        """验证多线程同时自增同一 session 的 interaction_seq 无竞态。

        10 个线程各执行 100 次自增,最终值应为 1000,
        且所有返回值唯一(无重复、无遗漏)。
        """
        idm = id_manager_cls()
        num_threads = 10
        increments_per_thread = 100
        results: list[int] = []
        results_lock = threading.Lock()

        def worker():
            local_results: list[int] = []
            for _ in range(increments_per_thread):
                seq = idm._increment_interaction_seq_in_pool("concurrent-session")
                local_results.append(seq)
            with results_lock:
                results.extend(local_results)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == num_threads * increments_per_thread
        assert len(set(results)) == len(results), "存在重复的 interaction_seq"
        assert min(results) == 0
        assert max(results) == num_threads * increments_per_thread - 1

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_concurrent_different_sessions_no_interference(id_manager_cls):
        """验证多线程操作不同 session 时互不干扰。

        10 个线程各操作自己的 session,每个 session 独立计数。
        """
        idm = id_manager_cls()
        num_threads = 10
        increments_per_session = 50
        results: dict[str, int] = {}
        results_lock = threading.Lock()

        def worker(session_id: str):
            last_seq = -1
            for _ in range(increments_per_session):
                seq = idm._increment_interaction_seq_in_pool(session_id)
                assert seq > last_seq, (
                    f"session {session_id} 序号非递增: {last_seq} -> {seq}"
                )
                last_seq = seq
            with results_lock:
                results[session_id] = last_seq

        threads = [
            threading.Thread(target=worker, args=(f"session-{i}",))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for sid, final_seq in results.items():
            assert final_seq == increments_per_session - 1, (
                f"session {sid} 最终值 {final_seq} 不等于期望 {increments_per_session - 1}"
            )

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_concurrent_mixed_read_write_no_race(id_manager_cls):
        """验证多线程混合读写(自增+读取)同一 session 无竞态。

        部分线程自增,部分线程读取。读取线程应始终读到合法值。
        """
        idm = id_manager_cls()
        session_id = "mixed-session"
        num_writers = 5
        num_readers = 5
        writes_per_writer = 50
        read_errors: list[str] = []
        errors_lock = threading.Lock()
        stop_flag = threading.Event()

        def writer():
            for _ in range(writes_per_writer):
                idm._increment_interaction_seq_in_pool(session_id)

        def reader():
            while not stop_flag.is_set():
                seq = idm._get_interaction_seq_from_pool(session_id)
                if not isinstance(seq, int) or seq < -1:
                    with errors_lock:
                        read_errors.append(f"读到异常值: {seq}")

        writers = [threading.Thread(target=writer) for _ in range(num_writers)]
        readers = [threading.Thread(target=reader) for _ in range(num_readers)]

        for r in readers:
            r.start()
        for w in writers:
            w.start()

        for w in writers:
            w.join()
        stop_flag.set()
        for r in readers:
            r.join()

        assert len(read_errors) == 0, f"读取线程发现异常: {read_errors}"

        final_seq = idm._get_interaction_seq_from_pool(session_id)
        assert final_seq == num_writers * writes_per_writer - 1

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_concurrent_lru_eviction_no_data_loss(id_manager_cls):
        """验证并发淘汰场景下不会丢失或损坏数据。

        15 个线程各操作 10 个 session(共 150 个 session,远超上限 20),
        每个 session 连续自增 5 次(中间不被其他 session 的淘汰打断)。
        池大小始终不超过上限,每个 session 连续自增期间序号单调递增。

        注意:LRU 淘汰意味着被淘汰的 session 重新访问时序号会重置为 0,
        这是 LRU 池的预期行为(旧 session 被遗忘)。本测试验证的是
        连续自增(不被淘汰打断)时序号正确递增,以及池大小不超限。
        """
        idm = id_manager_cls()
        id_manager_cls._MAX_SESSIONS = 20

        try:
            num_threads = 15
            sessions_per_thread = 10
            increments_per_session = 5
            errors: list[str] = []
            errors_lock = threading.Lock()

            def worker(thread_id: int):
                for s in range(sessions_per_thread):
                    sid = f"t{thread_id}-s{s}"
                    prev = -1
                    for _ in range(increments_per_session):
                        seq = idm._increment_interaction_seq_in_pool(sid)
                        # 同一 session 连续自增期间序号应递增。
                        # 如果 session 在自增过程中被其他线程淘汰,
                        # 重新创建后从 0 开始,这是 LRU 的预期行为,
                        # 不视为数据损坏。
                        if seq <= prev and seq != 0:
                            with errors_lock:
                                errors.append(
                                    f"session {sid} 序号异常: {prev} -> {seq}"
                                )
                        prev = seq

            threads = [
                threading.Thread(target=worker, args=(i,))
                for i in range(num_threads)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(idm._session_interaction_seqs) <= 20
            assert len(errors) == 0, f"发现数据不一致: {errors}"
        finally:
            id_manager_cls._MAX_SESSIONS = 100

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_empty_session_id_uses_special_key(id_manager_cls):
        """验证 session_id 为空时使用特殊 key,不与其他 session 冲突。"""
        idm = id_manager_cls()

        seq_empty = idm._increment_interaction_seq_in_pool("")
        assert seq_empty == 0

        seq_real = idm._increment_interaction_seq_in_pool("real-session")
        assert seq_real == 0

        # 两者互不干扰
        assert idm._get_interaction_seq_from_pool("") == 0
        assert idm._get_interaction_seq_from_pool("real-session") == 0

        # 空 session 再次自增
        seq_empty_2 = idm._increment_interaction_seq_in_pool("")
        assert seq_empty_2 == 1

        # 真 session 不受影响
        assert idm._get_interaction_seq_from_pool("real-session") == 0
