# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo HITL ask_user 卡片 request_id 派生与序号计数收口。

收口两类逻辑（derive + seq 计数），供 fresh / resume 两条发卡路径共用：
- ``derive_outer_call_id``：从入站 ``params.request_id`` 剥 ``{id}#{n}`` 后缀得外层 call id
- ``next_final_id``：同 call id 第 N 次中断加 ``{id}#{n}`` 后缀；首次返回原 id

⛔ 语义红线：
  - 序号计数必须挂在 adapter 实例上跨请求存续（同一外层 call id 跨请求递增）；
  - 作答回传靠 ``_build_interactive_input_from_answers`` 剥后缀对齐 harness 原始 id；
  - 不得改成请求级集合（R3 变种）。

挂起表复用 / 卡片实例登记 / 中断相位管理仍在 adapter 内（stale-guard 体系，
深度耦合 adapter 实例状态，不在此收口）。
"""

from __future__ import annotations

import re

__all__ = ["OuterCallIDDeduplicator"]


class OuterCallIDDeduplicator:
    """跨请求存续的 ask_user 卡片序号器。

    仅收口 derive（剥后缀）+ seq 计数；挂起表复用 / 实例登记 / 相位管理
    仍在 adapter 内（stale-guard 体系，深度耦合实例状态不外抽）。

    实例须由 adapter 在初始化时持有，跨请求存续；``_ask_user_card_seq`` 字典
    作为本对象的 thin delegate 暴露，守护直接断言该属性的测试不破。
    """

    _SUFFIX_RE = re.compile(r"^(?P<base>.+)#\d+$")

    def __init__(self) -> None:
        self._card_seq: dict[str, int] = {}

    @property
    def card_seq(self) -> dict[str, int]:
        """Thin delegate：守护直接断言 ``adapter._ask_user_card_seq`` 的测试。

        返回内部 dict 本体（非拷贝），与原 ``self._ask_user_card_seq`` 行为一致；
        保留对 ``adapter._ask_user_card_seq[tcid] = N`` 形式的直接赋值兼容。
        """
        return self._card_seq

    @card_seq.setter
    def card_seq(self, value: dict[str, int]) -> None:
        """替换序号 dict（adapter ``_ask_user_card_seq`` setter 的薄委托）。"""
        self._card_seq = value

    @classmethod
    def derive_outer_call_id(cls, params_request_id: str) -> str:
        """从入站 ``params.request_id`` 剥 ``{id}#{n}`` 后缀得外层 call id。

        与 ``interface._build_inputs`` 剥 ``{id}#{n}`` 后缀同一套规则：有后缀
        取 base，无后缀保留原值（不返回空串）。resume 内二次中断的 ask_user
        卡片需用此 id 作 request_id，对齐首个 ask_user（TIE 重写后用外层 call id）。
        """
        rid = (params_request_id or "").strip()
        m = cls._SUFFIX_RE.search(rid)
        return (m.group("base").strip() if m and m.group("base").strip() else rid)

    def next_final_id(self, request_id: str) -> str:
        """同 call id 第 N 次中断加 ``{id}#{n}`` 后缀；首次返回原 id。

        序号计数跨请求存续（挂在 adapter 实例上）。与现状 ``_dedupe_ask_user_card``
        的序号段逐字一致：``seq = self._ask_user_card_seq.get(request_id, 0)``；
        ``final_id = request_id if seq == 0 else f"{request_id}#{seq + 1}"``。

        ⛔ 调用方负责在 adapter 实例上持有本对象，跨请求存续。
        """
        seq = self._card_seq.get(request_id, 0)
        self._card_seq[request_id] = seq + 1
        return request_id if seq == 0 else f"{request_id}#{seq + 1}"
