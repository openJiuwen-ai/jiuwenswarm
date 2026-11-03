# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""从 ``[TodoProgress] run_end`` 日志计算 todo 进度指标（按模型分组）。

用法：
    python scripts/todo_progress_stats.py <日志文件或目录> [...]
    python scripts/todo_progress_stats.py --self-test

指标口径：
- 同一条 run_end 可能同时写进多个日志文件（分类日志、full.log、桌面端汇总日志），
  按 run_id 去重后再统计；没有 run_id 的记录无法去重，原样计入并单独报告条数。
- 正常结束残留率：有效样本 = result_type 为 answer、本轮至少成功写过一次 todo
  （writes_ok > 0）、且结束快照读取成功的 invoke；分子 = 有效样本中结束时仍有
  pending/in_progress 的数量。结束快照为 unknown 的不进分子分母；分母为 0 记"不可计算"。
- 未写入但留有未完成项：result_type 为 answer、本轮没有成功写过 todo、结束时列表里
  还有未完成项的 invoke 数，单独报告。这类执行可能是用户转去做别的、旧列表没动
  （正常），也可能是接着做原任务却一直没更新（问题），日志无法区分，所以不进残留率。
- 结束快照 unknown 单独报告：先按全部 invoke 计数（异常结束、用户取消也算），再给出
  其中 answer 的条数（只有这部分会影响残留率）。不看 had_todos：快照一直读取失败时
  had_todos 也无法确认。
- 写入失败率 = writes_failed / (writes_ok + writes_failed)。
- 无变化写入率 = noops / (changes + noops)；change_unknown 单独报告。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_MARK = "[TodoProgress] run_end "
_UNFINISHED = ("pending", "in_progress")


@dataclass
class Stats:
    runs: int = 0
    residual_num: int = 0
    residual_den: int = 0
    untouched_unfinished: int = 0     # answer 轮中本轮没写过 todo、结束时仍有未完成项的次数
    end_unknown: int = 0              # 全部 invoke 中结束快照读取失败的次数
    end_unknown_answer: int = 0       # 其中 result_type 为 answer 的次数
    writes_ok: int = 0
    writes_failed: int = 0
    changes: int = 0
    noops: int = 0
    change_unknown: int = 0
    rail_errors: int = 0
    iterations: list[int] = field(default_factory=list)

    def add(self, rec: dict[str, Any]) -> None:
        self.runs += 1
        self.writes_ok += int(rec.get("writes_ok", 0))
        self.writes_failed += int(rec.get("writes_failed", 0))
        self.changes += int(rec.get("changes", 0))
        self.noops += int(rec.get("noops", 0))
        self.change_unknown += int(rec.get("change_unknown", 0))
        self.rail_errors += int(rec.get("rail_errors", 0))
        if rec.get("result_type") == "answer":
            self.iterations.append(int(rec.get("iteration", 0)))
        is_answer = rec.get("result_type") == "answer"
        final = rec.get("final")
        if not isinstance(final, dict):
            self.end_unknown += 1
            self.end_unknown_answer += int(is_answer)
            return
        if not is_answer:
            return
        left = any(int(final.get(s, 0)) > 0 for s in _UNFINISHED)
        if int(rec.get("writes_ok", 0)) <= 0:
            self.untouched_unfinished += int(left)
            return
        self.residual_den += 1
        self.residual_num += int(left)


def parse_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    records = []
    for line in lines:
        idx = line.find(_MARK)
        if idx < 0:
            continue
        try:
            records.append(json.loads(line[idx + len(_MARK):].strip()))
        except json.JSONDecodeError:
            continue
    return records


@dataclass
class Dedup:
    records: list[dict[str, Any]]
    duplicates: int = 0
    without_run_id: int = 0


def dedupe(records: Iterable[dict]) -> Dedup:
    """按 run_id 去重；没有 run_id 的记录原样保留。"""
    seen: set[str] = set()
    out = Dedup(records=[])
    for rec in records:
        run_id = rec.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            out.without_run_id += 1
            out.records.append(rec)
        elif run_id in seen:
            out.duplicates += 1
        else:
            seen.add(run_id)
            out.records.append(rec)
    return out


def aggregate(records: Iterable[dict[str, Any]]) -> dict[str, Stats]:
    groups: dict[str, Stats] = defaultdict(Stats)
    for rec in records:
        groups[str(rec.get("model") or "unknown")].add(rec)
        groups["(全部)"].add(rec)
    return dict(groups)


def ratio(num: int, den: int) -> str:
    return "不可计算" if den == 0 else f"{num}/{den} = {num / den:.1%}"


def median(values: list[int]) -> str:
    return "不可计算" if not values else f"{statistics.median(values):g}（n={len(values)}）"


def render(groups: dict[str, Stats]) -> str:
    out = []
    for model in sorted(groups, key=lambda m: (m == "(全部)", m)):
        s = groups[model]
        out.append(f"== {model}  runs={s.runs}  rail_errors={s.rail_errors}")
        out.append(f"   正常结束残留率  {ratio(s.residual_num, s.residual_den)}  结束快照 unknown={s.end_unknown}（answer {s.end_unknown_answer}）")
        out.append(f"   未写入但留有未完成项 {s.untouched_unfinished}")
        out.append(f"   写入失败率      {ratio(s.writes_failed, s.writes_ok + s.writes_failed)}")
        out.append(f"   无变化写入率    {ratio(s.noops, s.changes + s.noops)}  change_unknown={s.change_unknown}")
        out.append(f"   answer 轮中位数 iteration={median(s.iterations)}")
    return "\n".join(out)


def _iter_files(paths: list[str]) -> Iterable[Path]:
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            yield from sorted(f for f in p.rglob("*") if f.is_file() and f.suffix in {".log", ".txt", ""})
        elif p.is_file():
            yield p


def _line(**fields: Any) -> str:
    return "2026-11-02 10:00:00.000 INFO x y.py:1: " + _MARK + json.dumps(fields)


def self_test() -> None:
    base = dict(writes_ok=0, writes_failed=0, changes=0, noops=0, change_unknown=0, rail_errors=0)
    done = {"pending": 0, "in_progress": 0, "completed": 2, "cancelled": 0}
    left = {"pending": 1, "in_progress": 1, "completed": 0, "cancelled": 0}
    # 样例一：含结束快照 unknown 的执行
    sample1 = [
        _line(model="M", result_type="answer", had_todos=True, final=done, iteration=4, **{**base, "writes_ok": 3, "changes": 2, "noops": 1}),
        _line(model="M", result_type="answer", had_todos=True, final=left, **{**base, "writes_ok": 1, "writes_failed": 1, "changes": 1}),
        _line(model="M", result_type="answer", had_todos=True, final="unknown", **{**base, "writes_ok": 1, "change_unknown": 1, "rail_errors": 1}),
        _line(model="M", result_type="interrupt", had_todos=True, final=left, **base),
        _line(model="M", result_type="answer", had_todos=False, final=done, **base),
        # 本轮没写过 todo：不进残留率，结束时还有未完成项的单列
        _line(model="M", result_type="answer", had_todos=True, final=left, **base),
        # 文件一开始就损坏：快照一直读不到，had_todos 为假，仍要计入结束快照 unknown
        _line(model="M", result_type="answer", had_todos=False, final="unknown", **{**base, "rail_errors": 2}),
        "unrelated log line",
    ]
    s = aggregate(parse_lines(sample1))["M"]
    assert (s.runs, s.residual_num, s.residual_den, s.end_unknown, s.end_unknown_answer) == (7, 1, 2, 2, 2), s
    assert s.untouched_unfinished == 1
    assert ratio(s.writes_failed, s.writes_ok + s.writes_failed) == "1/6 = 16.7%"
    assert ratio(s.noops, s.changes + s.noops) == "1/4 = 25.0%" and s.change_unknown == 1
    assert s.rail_errors == 3
    assert median(s.iterations) == "0（n=6）" and median([100, 300]) == "200（n=2）"
    # 样例二：有效分母为 0
    sample2 = [
        _line(model="N", result_type="answer", had_todos=True, final="unknown", **base),
        _line(model="N", result_type="error", had_todos=True, final=left, **base),
        # 异常结束、用户取消时结束快照读取失败：计入 unknown，但不算 answer
        _line(model="N", result_type="error", had_todos=False, final="unknown", **base),
        _line(model="N", result_type="unknown", had_todos=True, final="unknown", **base),
    ]
    s2 = aggregate(parse_lines(sample2))["N"]
    assert (s2.residual_den, s2.end_unknown, s2.end_unknown_answer) == (0, 3, 1)
    assert ratio(s2.residual_num, s2.residual_den) == "不可计算"
    assert ratio(s2.noops, s2.changes + s2.noops) == "不可计算"
    assert median([]) == "不可计算"
    # 样例三：同一条 run_end 出现在多个日志文件
    one = _line(model="K", run_id="r1", result_type="answer", had_todos=True, final=done, **{**base, "writes_ok": 4})
    wrote = {**base, "writes_ok": 1}
    sample3 = [one, one, one,
               _line(model="K", run_id="r2", result_type="answer", had_todos=True, final=left, **wrote),
               _line(model="K", result_type="answer", had_todos=True, final=left, **wrote)]
    d = dedupe(parse_lines(sample3))
    assert (len(d.records), d.duplicates, d.without_run_id) == (3, 2, 1)
    s3 = aggregate(d.records)["K"]
    assert (s3.runs, s3.writes_ok, s3.residual_num, s3.residual_den) == (3, 6, 2, 3)
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="日志文件或目录")
    parser.add_argument("--self-test", action="store_true", help="用内置样例校验指标口径")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if not args.paths:
        parser.error("需要日志文件或目录")
    records: list[dict[str, Any]] = []
    for f in _iter_files(args.paths):
        with open(f, encoding="utf-8", errors="replace") as fh:
            records.extend(parse_lines(fh))
    if not records:
        print("未找到 [TodoProgress] run_end 记录")
        return 1
    result = dedupe(records)
    print(f"读取 {len(records)} 条 run_end，按 run_id 去掉重复 {result.duplicates} 条，"
          f"无 run_id 未去重 {result.without_run_id} 条")
    print(render(aggregate(result.records)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
