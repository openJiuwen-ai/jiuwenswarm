# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""配置写锁守门测试（锁收敛系列批次 0）：防止新增绕过 update_config 的裸写函数。

config.yaml 被 AgentServer 与 Gateway 两个进程并发读写，模块声明的唯一原子写
入口是 ``update_config``（threading.Lock + portalocker 双层锁，整个
load→mutate→dump 为原子临界区）。当前仍有 36 个写函数绕过该锁自行
"读全文→改→写回全文"（豁免白名单见 ``BYPASS_ALLOWLIST``）：两个写者的
读-改-写区间重叠时，后写者以前写者读到的旧全文为基础覆盖整个文件，
前写者的修改静默丢失（无报错、无日志）。

本文件是锁收敛系列（共 5 批）的永久 CI 门禁：

1. 静态守门 —— 白名单外的任何直接落盘函数立即变红（防新增绕锁）；
   白名单内的函数完成改造后必须移出（防漏划，双向断言）。
2. 并发回归 —— ``update_config`` 双写者并发不丢更新。
3. 机理复现 —— 旧裸写模式 + Barrier 确定性复现丢更新，固化缺陷机理。
4. 再入防护 —— mutator 内嵌套调用应立即 RuntimeError（而非阻塞超时后 TimeoutError）。

注：``set_config`` 走 ``yaml.safe_dump`` 裸写（还会抹注释），不属于
``dump_yaml_round_trip`` 家族，由独立修复项处理，不在本守门范围。
"""

from __future__ import annotations

import inspect
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

import jiuwenswarm.common.config as cfg_mod

# 当前仍绕过 update_config 直接落盘的函数（锁收敛系列逐批改造、逐批移出）。
BYPASS_ALLOWLIST = {
    # 批次 1：IM 通道族（网关高频路径）
    "update_channel_in_config",
    "update_xiaoyi_runtime_in_config",
    "update_channel_subsection_in_config",
    "replace_channel_subsection_with_cleanup",
    "update_channel_app_field",
    "update_preferred_language_in_config",
    # 批次 2：功能开关族
    "set_auto_memory_enabled",
    "update_browser_in_config",
    "update_context_engine_enabled_in_config",
    "update_symphony_in_config",
    "update_skill_retrieval_in_config",
    "update_auto_recap_enabled_in_config",
    "update_proactive_recommendation_in_config",
    "update_trajectory_ui_in_config",
    "update_task_full_duplex_in_config",
    "update_task_asr_in_config",
    "update_updater_in_config",
    "update_a2ui_in_config",
    # 批次 3：模型/团队/swarmflow/外部 CLI 族
    "update_default_model_provider_in_config",
    "replace_teams_in_config",
    "update_swarmflow_enabled_in_config",
    "update_external_cli_agents_in_config",
    "update_external_cli_builtin_models_in_config",
    "update_swarmflow_budget_in_config",
    # 批次 4：MCP/subagent/记忆禁用/sandbox 族
    "upsert_mcp_server_in_config",
    "set_mcp_server_enabled_in_config",
    "remove_mcp_server_in_config",
    "upsert_subagent_in_config",
    "remove_subagent_from_config",
    "update_memory_forbidden_enabled_in_config",
    "update_memory_forbidden_description_in_config",
    "update_memory_forbidden_in_config",
    "update_sandbox_endpoint",
    "update_sandbox_runtime",
    # 特殊项：写的是传入的 config_path（init 场景）与启动期模板迁移，另行立项加锁
    "set_preferred_language_in_config_file",
    "migrate_config_from_template",
}


def _seed(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {"seed_section": {"kept": True}}, allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )


@pytest.fixture
def patched_config(tmp_path: Path, monkeypatch) -> Path:
    cfg = tmp_path / "config.yaml"
    _seed(cfg)
    monkeypatch.setattr(cfg_mod, "CONFIG_YAML_PATH", cfg)
    monkeypatch.setattr(cfg_mod, "_CONFIG_YAML_PATH", cfg)
    return cfg


def test_no_new_unlocked_config_writers() -> None:
    """直接调用 dump_yaml_round_trip 的模块级函数必须持锁或经 update_config。

    - 白名单外出现直接落盘函数 → 失败：禁止新增绕锁写函数。
    - 白名单内函数源码已含锁调用 → 失败：已完成收敛，请从白名单移除。
    """
    offenders: list[str] = []
    for name, obj in sorted(vars(cfg_mod).items()):
        if not inspect.isfunction(obj) or obj.__module__ != cfg_mod.__name__:
            continue
        if name in ("dump_yaml_round_trip", "_dump_yaml_round_trip", "update_config"):
            continue
        source = inspect.getsource(obj)
        if (
            "dump_yaml_round_trip(" not in source
            and "_dump_yaml_round_trip(" not in source
        ):
            continue
        holds_lock = ("config_write_lock(" in source) or ("update_config(" in source)
        if name in BYPASS_ALLOWLIST:
            if holds_lock:
                offenders.append(f"{name}（已改造，请从 BYPASS_ALLOWLIST 移除）")
        elif not holds_lock:
            offenders.append(
                f"{name}（绕过写锁，请改走 update_config 或加入白名单并说明理由）"
            )
    assert not offenders, "配置写锁守门失败:\n" + "\n".join(f"- {o}" for o in offenders)


def test_concurrent_update_config_no_lost_update(patched_config: Path) -> None:
    """两个线程各写不同键，50 轮并发后双方修改必须全部存活。"""
    for round_no in range(50):

        def _write(key: str, value: str) -> None:
            def _mutate(data: dict[str, Any]) -> dict[str, Any]:
                data.setdefault("concurrency_probe", {})[key] = value
                return data

            cfg_mod.update_config(_mutate)

        writer_a = threading.Thread(target=_write, args=("writer_a", f"a{round_no}"))
        writer_b = threading.Thread(target=_write, args=("writer_b", f"b{round_no}"))
        writer_a.start()
        writer_b.start()
        writer_a.join(timeout=30)
        writer_b.join(timeout=30)

    data = yaml.safe_load(patched_config.read_text(encoding="utf-8"))
    probe = data["concurrency_probe"]
    assert probe["writer_a"] == "a49"
    assert probe["writer_b"] == "b49"


def test_bypass_pattern_reproduces_lost_update(tmp_path: Path) -> None:
    """旧裸写模式（锁外读全文→改→写回全文）+ Barrier 确定性复现丢更新。

    Barrier 保证两个写者都完成"读"之后才允许"写"：后写者以前写者读到的
    旧全文为基底覆盖整个文件，先写者的键被抹掉。该用例固化缺陷机理，
    说明锁收敛系列为什么要改造白名单内的函数。
    """
    cfg = tmp_path / "demo.yaml"
    _seed(cfg)
    barrier = threading.Barrier(2)

    def _bypass_writer(key: str, value: str) -> None:
        data = cfg_mod.load_yaml_round_trip(cfg)  # 锁外读
        barrier.wait(timeout=5)  # 双方都读完旧全文才放行
        data.setdefault("demo", {})[key] = value
        cfg_mod.dump_yaml_round_trip(cfg, data)  # 锁外整文件写回

    first = threading.Thread(target=_bypass_writer, args=("first_key", "v1"))
    second = threading.Thread(target=_bypass_writer, args=("second_key", "v2"))
    first.start()
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert len(data.get("demo", {})) == 1, (
        "裸写模式并发下应恰好只剩一个键（丢更新确实发生）"
    )


def test_reentrant_mutator_fails_fast(patched_config: Path) -> None:
    """mutator 内嵌套调用 update_config 应立即 RuntimeError。

    双层锁不可重入：改造前同线程重入会阻塞 lock_timeout（默认 10s）后才抛
    TimeoutError；批次 0 引入同线程重入检测后立即快速失败，便于及早暴露
    mutator 嵌套调用其它 *_in_config 写函数的错误用法。
    """

    def _bad_mutator(data: dict[str, Any]) -> dict[str, Any]:
        cfg_mod.update_config(lambda inner: inner)  # 再入！
        return data

    with pytest.raises(RuntimeError, match="re-entered"):
        cfg_mod.update_config(_bad_mutator)


def test_comments_survive_update_config(patched_config: Path) -> None:
    """update_config 走 ruamel round-trip，注释必须在写回后逐行保留。

    防止锁收敛改造期间误换 yaml.safe_dump（会抹掉全部注释）。
    """
    patched_config.write_text(
        "# 顶部注释：勿删\nseed_section:\n  kept: true\n",
        encoding="utf-8",
    )

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        data.setdefault("concurrency_probe", {})["n"] = 1
        return data

    for _ in range(3):
        cfg_mod.update_config(_mutate)

    text = patched_config.read_text(encoding="utf-8")
    assert "# 顶部注释：勿删" in text
    assert "kept: true" in text
