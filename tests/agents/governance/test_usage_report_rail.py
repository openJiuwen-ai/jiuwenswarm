# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for UsageReportRail.

Runtime-free: fire ``after_model_call`` / ``after_invoke`` with synthetic
contexts carrying fake responses, and assert per-stage token accumulation,
the total-tokens fallback, unlabeled routing, cross-invoke merge without
double counting, and JSON persistence.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.usage_report_rail import (
    UsageReportRail,
    read_usage_tokens,
)


def _resp(*, inp=0, out=0, total=None):
    usage = types.SimpleNamespace(input_tokens=inp, output_tokens=out)
    if total is not None:
        usage.total_tokens = total
    return types.SimpleNamespace(usage_metadata=usage)


def _model_ctx(response, *, stage_label=None) -> AgentCallbackContext:
    extra = {}
    if stage_label is not None:
        extra["stage_label"] = stage_label
    return AgentCallbackContext(
        agent=object(),
        inputs=ModelCallInputs(messages=[], tools=None, model_context=None, response=response),
        extra=extra,
    )


def _invoke_ctx() -> AgentCallbackContext:
    return AgentCallbackContext(agent=object(), inputs=object(), extra={})


class TestReadUsageTokens(unittest.TestCase):
    def test_prefers_total(self) -> None:
        self.assertEqual(read_usage_tokens(_resp(inp=10, out=5, total=15)), (10, 5, 15))

    def test_fallback_to_sum_when_total_absent(self) -> None:
        self.assertEqual(read_usage_tokens(_resp(inp=5, out=3)), (5, 3, 8))

    def test_fallback_when_total_zero(self) -> None:
        self.assertEqual(read_usage_tokens(_resp(inp=5, out=3, total=0)), (5, 3, 8))

    def test_no_usage_is_zero(self) -> None:
        self.assertEqual(read_usage_tokens(types.SimpleNamespace()), (0, 0, 0))
        self.assertEqual(read_usage_tokens(None), (0, 0, 0))


class TestUsageReportRail(unittest.IsolatedAsyncioTestCase):
    async def test_accumulate_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nested" / "usage.json"
            rail = UsageReportRail(path)
            await rail.after_model_call(_model_ctx(_resp(inp=10, out=5, total=15), stage_label="Scout"))
            await rail.after_model_call(_model_ctx(_resp(inp=20, out=10, total=30), stage_label="Scout"))
            await rail.after_invoke(_invoke_ctx())

            report = json.loads(path.read_text(encoding="utf-8"))
            scout = report["stages"]["Scout"]
            self.assertEqual(scout["calls"], 2)
            self.assertEqual(scout["input_tokens"], 30)
            self.assertEqual(scout["output_tokens"], 15)
            self.assertEqual(scout["total_tokens"], 45)
            self.assertEqual(report["grand_total"]["total_tokens"], 45)
            self.assertEqual(report["invoke_count"], 1)
            # tallies reset after persist
            self.assertEqual(rail.stages, {})

    async def test_total_fallback_counted(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.json"
            rail = UsageReportRail(path)
            await rail.after_model_call(_model_ctx(_resp(inp=5, out=3), stage_label="Write"))
            await rail.after_invoke(_invoke_ctx())
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["stages"]["Write"]["total_tokens"], 8)

    async def test_unlabeled_routing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.json"
            rail = UsageReportRail(path)
            await rail.after_model_call(_model_ctx(_resp(inp=1, out=1, total=2)))  # no label
            await rail.after_invoke(_invoke_ctx())
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("unlabeled", report["stages"])
            self.assertEqual(report["stages"]["unlabeled"]["calls"], 1)

    async def test_multi_invoke_merge_no_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.json"
            rail = UsageReportRail(path)
            # invoke 1: stage Scout
            await rail.after_model_call(_model_ctx(_resp(inp=10, out=5, total=15), stage_label="Scout"))
            await rail.after_invoke(_invoke_ctx())
            # invoke 2: stage Ideate (same rail instance reused)
            await rail.after_model_call(_model_ctx(_resp(inp=4, out=2, total=6), stage_label="Ideate"))
            await rail.after_invoke(_invoke_ctx())

            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["stages"]["Scout"]["total_tokens"], 15)
            self.assertEqual(report["stages"]["Ideate"]["total_tokens"], 6)
            self.assertEqual(report["grand_total"]["total_tokens"], 21)
            self.assertEqual(report["invoke_count"], 2)

    async def test_separate_instances_merge_into_same_file(self) -> None:
        # Two members (two rail instances) writing the same report file.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.json"
            rail_a = UsageReportRail(path)
            await rail_a.after_model_call(_model_ctx(_resp(inp=10, out=5, total=15), stage_label="Scout"))
            await rail_a.after_invoke(_invoke_ctx())

            rail_b = UsageReportRail(path)
            await rail_b.after_model_call(_model_ctx(_resp(inp=7, out=3, total=10), stage_label="Scout"))
            await rail_b.after_invoke(_invoke_ctx())

            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["stages"]["Scout"]["total_tokens"], 25)
            self.assertEqual(report["invoke_count"], 2)

    async def test_no_usage_still_counts_call(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.json"
            rail = UsageReportRail(path)
            await rail.after_model_call(_model_ctx(types.SimpleNamespace(usage_metadata=None), stage_label="Plan"))
            await rail.after_invoke(_invoke_ctx())
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["stages"]["Plan"]["calls"], 1)
            self.assertEqual(report["stages"]["Plan"]["total_tokens"], 0)

    async def test_report_write_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.json"
            rail = UsageReportRail(path)
            await rail.after_model_call(_model_ctx(_resp(inp=1, out=1, total=2), stage_label="Scout"))
            await rail.after_invoke(_invoke_ctx())

            # No leftover temp/lock artifacts beyond the report and its sidecar lock.
            leftovers = {p.name for p in Path(d).iterdir()} - {path.name, path.name + ".lock"}
            self.assertEqual(leftovers, set())

            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["stages"]["Scout"]["total_tokens"], 2)

    async def test_corrupt_report_preserved_not_reset(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.json"
            corrupt_content = '{"truncated'
            path.write_text(corrupt_content, encoding="utf-8")
            rail = UsageReportRail(path)

            with self.assertLogs(
                "jiuwenswarm.agents.harness.common.rails.usage_report_rail", level="WARNING"
            ) as cm:
                await rail.after_model_call(_model_ctx(_resp(inp=1, out=1, total=2), stage_label="Scout"))
                await rail.after_invoke(_invoke_ctx())

            self.assertTrue(any("unreadable" in msg for msg in cm.output))

            corrupt_files = list(Path(d).glob("usage.json.corrupt-*"))
            self.assertEqual(len(corrupt_files), 1)
            self.assertEqual(corrupt_files[0].read_text(encoding="utf-8"), corrupt_content)

            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["stages"]["Scout"]["total_tokens"], 2)

    async def test_concurrent_merges_do_not_lose_updates(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.json"

            def _record_one(i: int) -> None:
                rail = UsageReportRail(path)
                asyncio.run(
                    rail.after_model_call(_model_ctx(_resp(inp=0, out=0, total=1), stage_label=f"stage{i % 2}"))
                )
                asyncio.run(rail.after_invoke(_invoke_ctx()))

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(_record_one, range(8)))

            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["grand_total"]["total_tokens"], 8)



class TestWriteFailureIsLoggedNotSilent(unittest.IsolatedAsyncioTestCase):
    """A write that cannot happen must say so, and must not corrupt what follows.

    Reporting is best-effort: the failed invoke is left out of the report. The
    defect this guards against is losing it *quietly*, which would make the
    report understate spend with nothing to show why.
    """

    async def test_failure_is_logged_and_later_invokes_still_report(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "usage.json"
            rail = UsageReportRail(str(report))

            await rail.after_model_call(
                _model_ctx(_resp(inp=10, out=0, total=10), stage_label="draft"))
            with mock.patch("os.replace", side_effect=OSError("disk full")):
                with self.assertLogs(
                    "jiuwenswarm.agents.harness.common.rails.usage_report_rail",
                    level="ERROR",
                ) as logs:
                    await rail.after_invoke(_invoke_ctx())
            self.assertTrue(any("write failed" in line for line in logs.output),
                            "a failed write must be reported in the log")
            self.assertFalse(report.exists())

            await rail.after_model_call(
                _model_ctx(_resp(inp=20, out=0, total=20), stage_label="draft"))
            await rail.after_invoke(_invoke_ctx())

            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["grand_total"]["total_tokens"], 20)
            self.assertEqual(data["invoke_count"], 1)


class TestConcurrentFlush(unittest.IsolatedAsyncioTestCase):
    """Two invokes on one instance must not both bill the same tallies.

    Writing runs on a worker thread, so ``after_invoke`` yields. The tallies are
    detached in a single statement before that happens, so the second invoke
    cannot pick up what the first already took.
    """

    async def test_two_concurrent_invokes_bill_once(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "usage.json"
            rail = UsageReportRail(str(report))

            await rail.after_model_call(
                _model_ctx(_resp(inp=10, out=0, total=10), stage_label="draft"))
            await rail.after_model_call(
                _model_ctx(_resp(inp=20, out=0, total=20), stage_label="draft"))

            await asyncio.gather(
                rail.after_invoke(_invoke_ctx()),
                rail.after_invoke(_invoke_ctx()),
            )

            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["grand_total"]["total_tokens"], 30,
                             "tallies were billed more than once")
            self.assertEqual(data["invoke_count"], 2)


if __name__ == "__main__":
    unittest.main()
