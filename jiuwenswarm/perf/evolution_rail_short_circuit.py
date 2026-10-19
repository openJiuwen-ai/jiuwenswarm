# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""[PERF 实验] 企业版 + symphony 关闭时短路 evolution rail 轨迹 drain。

实测(py-spy, N=1 连续轮次): EvolutionRail.after_model_call 占回合时间 ~40%
(drain/enrich/merge 轨迹管道),而 symphony.enabled=false 时这些数据无处可去。
本补丁仅在企业版生效;个人版不短路,保证自演进轨迹路径完整。
企业版在 symphony 主开关关闭时将 before_invoke / after_model_call /
after_tool_call / after_task_iteration 短路为 no-op;生产化修复应在核心侧
"未启用即不订阅"。

注意: before_invoke 现已加入短路列表。symphony 关闭时短路 before_invoke
会导致 self._builder 为 None, 但 after_invoke 有 None 守卫会安全返回,
自演进不触发是预期行为（symphony 关闭时轨迹数据无处可去）。
"""

import logging

logger = logging.getLogger(__name__)

_APPLIED = False


def apply_evolution_rail_short_circuit() -> None:
    global _APPLIED
    if _APPLIED:
        return

    from jiuwenswarm.edition import is_enterprise

    if not is_enterprise():
        _APPLIED = True
        logger.info("[evolution_short_circuit] 个人版跳过: 不短路 evolution rail")
        return

    try:
        from openjiuwen.harness.rails.evolution.evolution_rail import EvolutionRail
    except Exception:  # noqa: BLE001 - 核心无该 rail 时静默跳过
        logger.debug("[evolution_short_circuit] EvolutionRail 不可用,跳过")
        return

    def _make_short_circuited(name: str, orig):
        async def _short_circuited(self, ctx):  # noqa: ANN001, ANN202
            try:
                from jiuwenswarm.symphony.config import load_symphony_config

                cfg = load_symphony_config()
                if not cfg.enabled:
                    return None  # symphony 未启用:轨迹 drain 短路
            except Exception:  # noqa: BLE001 - 判定失败走原路径
                pass
            return await orig(self, ctx)

        _short_circuited.__name__ = name
        return _short_circuited

    # symphony 关闭时仍短路 drain 路径以省回合时延。
    for _name in (
        "before_invoke",
        "after_model_call",
        "after_tool_call",
        "after_task_iteration",
    ):
        _orig = getattr(EvolutionRail, _name, None)
        if _orig is not None:
            setattr(EvolutionRail, _name, _make_short_circuited(_name, _orig))
    _APPLIED = True
    logger.info("[evolution_short_circuit] 已应用(企业版): symphony 关闭时跳过轨迹 drain")
