# -*- coding: utf-8 -*-
"""检索 / 排序任务的**标准指标**——公式公认、可确定性计算，无需生成代码。

为什么需要这个模块（2026-09-17 静态审查 F-02/F-03）：

模块三的 ``manifest.metrics[].verified`` 原本**只有一处**会被置 True ——
``implementation_builder._standard_metric_definition`` 里那 12 个硬编码 sklearn
指标（accuracy / macro_f1 / mae / rmse …）。而 ``review_contracts`` 对任何
``verified=False`` 的指标直接报 error。于是**检索/排序类任务永远走不完**：
planner 只能自造 ``key_recall`` 这类指标名，而它们没有任何途径变成"已验证"。

本模块把 IR 领域的标准指标做成和 sklearn 那批**同一机制**：公式固定、无歧义、
纯函数、不依赖外部库，所以可以像 ``sklearn.metrics.accuracy_score`` 一样直接声明
"用这个实现算"。

设计约定
--------
- 所有函数签名统一为 ``(ranked, relevant, **params) -> float``：
  ``ranked`` 是**每个 query 的排序结果**（``list[list[str]]``，按相关性从高到低），
  ``relevant`` 是**每个 query 的 ground truth 集合**（``list[set[str]]``）。
- 纯函数、确定性：同样输入必得同样输出，不读时钟、不用随机数。
- 空输入返回 0.0 而不是抛异常：评测集为空是「指标为 0」不是「程序崩了」。
- 每个函数都带 ``@_metric`` 装饰器登记进 :data:`RETRIEVAL_METRICS`，
  ``implementation_builder`` 据此生成 manifest 里的实现声明。
"""
from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "RETRIEVAL_METRICS",
    "recall_at_k",
    "precision_at_k",
    "hit_rate_at_k",
    "mrr",
    "ndcg_at_k",
    "available_metric_names",
]


def _as_pairs(
    ranked: Sequence[Iterable[Any]], relevant: Sequence[Iterable[Any]]
) -> list[tuple[list[str], set[str]]]:
    """把输入规整成 ``[(排序列表, 相关集合)]``，逐条 str 化并去重。"""
    pairs: list[tuple[list[str], set[str]]] = []
    for order, truth in zip(ranked, relevant):
        seen: list[str] = []
        for item in order:
            token = str(item)
            if token not in seen:
                seen.append(token)
        pairs.append((seen, {str(item) for item in truth}))
    return pairs


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def recall_at_k(
    ranked: Sequence[Iterable[Any]],
    relevant: Sequence[Iterable[Any]],
    *,
    k: int = 10,
) -> float:
    """查全率：前 k 个结果里覆盖了多大比例的 ground truth。

    ``recall@k = |top_k ∩ relevant| / |relevant|``，对每个 query 求平均。
    ground truth 为空的 query 记 0（无可召回之物，不参与"命中"）。
    """
    if k <= 0:
        raise ValueError("k must be positive")
    scores: list[float] = []
    for order, truth in _as_pairs(ranked, relevant):
        if not truth:
            scores.append(0.0)
            continue
        scores.append(len(set(order[:k]) & truth) / len(truth))
    return _mean(scores)


def precision_at_k(
    ranked: Sequence[Iterable[Any]],
    relevant: Sequence[Iterable[Any]],
    *,
    k: int = 10,
) -> float:
    """查准率：前 k 个结果里有多少比例是相关的。

    分母固定为 ``k``（而不是"实际返回条数"）——返回不足 k 条时按空缺不计入，
    这是 IR 的通行口径，也让不同系统之间可比。
    """
    if k <= 0:
        raise ValueError("k must be positive")
    scores: list[float] = []
    for order, truth in _as_pairs(ranked, relevant):
        scores.append(len(set(order[:k]) & truth) / k)
    return _mean(scores)


def hit_rate_at_k(
    ranked: Sequence[Iterable[Any]],
    relevant: Sequence[Iterable[Any]],
    *,
    k: int = 10,
) -> float:
    """命中率：前 k 个结果里**至少有一个**相关的 query 占比。"""
    if k <= 0:
        raise ValueError("k must be positive")
    hits = [
        1.0 if (set(order[:k]) & truth) else 0.0
        for order, truth in _as_pairs(ranked, relevant)
    ]
    return _mean(hits)


def mrr(
    ranked: Sequence[Iterable[Any]],
    relevant: Sequence[Iterable[Any]],
) -> float:
    """平均倒数排名：第一个相关结果出现位置的倒数，对 query 求平均。"""
    scores: list[float] = []
    for order, truth in _as_pairs(ranked, relevant):
        reciprocal = 0.0
        if truth:
            for position, token in enumerate(order, start=1):
                if token in truth:
                    reciprocal = 1.0 / position
                    break
        scores.append(reciprocal)
    return _mean(scores)


def ndcg_at_k(
    ranked: Sequence[Iterable[Any]],
    relevant: Sequence[Iterable[Any]],
    *,
    k: int = 10,
) -> float:
    """归一化折损累计增益（二值相关性）。

    相关性只有 0/1 两档时的标准定义：``DCG@k / IDCG@k``，折损用 ``1/log2(rank+1)``。
    IDCG 取"理想排序"（相关项全部排在最前）的 DCG；无相关项时记 0。
    """
    if k <= 0:
        raise ValueError("k must be positive")
    scores: list[float] = []
    for order, truth in _as_pairs(ranked, relevant):
        dcg = sum(
            1.0 / math.log2(position + 1)
            for position, token in enumerate(order[:k], start=1)
            if token in truth
        )
        ideal_hits = min(len(truth), k)
        idcg = sum(1.0 / math.log2(position + 1) for position in range(1, ideal_hits + 1))
        scores.append(dcg / idcg if idcg else 0.0)
    return _mean(scores)


#: 指标名 → (函数, 是否需要 k, 说明)。``implementation_builder`` 从这里取实现声明。
#: 集中登记而不是用装饰器：装饰器会让阅读顺序倒置（函数定义处才登记），
#: 而这份表同时也是**对外可见的合法指标名清单**（blocker 文案要引用它）。
RETRIEVAL_METRICS: dict[str, tuple[Callable[..., float], bool, str]] = {
    "recall_at_k": (recall_at_k, True, "查全率：top-k 覆盖的 ground truth 比例"),
    "precision_at_k": (precision_at_k, True, "查准率：top-k 中相关结果占比"),
    "hit_rate_at_k": (hit_rate_at_k, True, "命中率：至少命中一个相关结果的 query 占比"),
    "mrr": (mrr, False, "平均倒数排名"),
    "ndcg_at_k": (ndcg_at_k, True, "归一化折损累计增益（二值相关性）"),
}


def available_metric_names() -> list[str]:
    """标准检索指标名（供 blocker 文案与 planning 侧门禁引用）。"""
    return sorted(RETRIEVAL_METRICS)
