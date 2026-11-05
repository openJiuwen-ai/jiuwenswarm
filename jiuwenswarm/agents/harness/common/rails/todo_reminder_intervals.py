# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""提醒区间记账（供 TodoProgressRail 使用）。

每次注入执行中提醒开始一个区间，到下一次注入或本次执行结束为止。区间结果按区间内
全部 todo 写入的变化判定取最强的一个：changed → followed_changed，否则 unknown →
followed_unknown，否则 noop → followed_noop，区间内没有写入 → no_write。只看注入后
第一次写入会把"先 noop、后 changed"误判为提醒无效，所以按整个区间统计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Change = Literal["changed", "noop", "unknown"]

_RANK = {"noop": 1, "unknown": 2, "changed": 3}
_OUTCOME = {"changed": "followed_changed", "unknown": "followed_unknown", "noop": "followed_noop"}


@dataclass
class ReminderIntervals:
    seq: int = 0                  # 本次执行已注入的次数，0 = 还没注入
    open: bool = False            # 是否有打开的区间
    inject_iter: int = 0          # 当前区间注入时所在迭代
    strongest: str = ""           # 当前区间内最强的变化判定
    reminded_since_write: bool = False  # 上一次写入之后有过注入
    counts: dict[str, int] = field(default_factory=dict)  # 区间结果 -> 区间数

    def start(self, iteration: int) -> None:
        self.close()
        self.seq += 1
        self.open = True
        self.inject_iter = iteration
        self.strongest = ""
        self.reminded_since_write = True

    def record_write(self, change: Change) -> bool:
        """记一次写入，返回 after_reminder：同一次注入只让它之后的第一次写入为真。"""
        after_reminder = self.reminded_since_write
        self.reminded_since_write = False
        if self.open and _RANK[change] > _RANK.get(self.strongest, 0):
            self.strongest = change
        return after_reminder

    def close(self) -> None:
        if not self.open:
            return
        key = _OUTCOME.get(self.strongest, "no_write")
        self.counts[key] = self.counts.get(key, 0) + 1
        self.open = False


__all__ = ["Change", "ReminderIntervals"]
