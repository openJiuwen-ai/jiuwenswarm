# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for skill sleep counter / rail / runner wiring."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.skill_sleep_rail import SkillSleepRail
from jiuwenswarm.agents.harness.common.skill_sleep.counter import SkillCallCounter
from jiuwenswarm.agents.harness.common.skill_sleep.runner import SkillSleepRunner
from jiuwenswarm.common.config import (
    get_skill_sleep_call_threshold,
    get_skill_sleep_enabled,
)


class TestSkillCallCounter(unittest.TestCase):
    def test_increment_persist_reset(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "call_counts.json"
            counter = SkillCallCounter(path)
            self.assertEqual(counter.increment("tianqi"), 1)
            self.assertEqual(counter.increment("tianqi"), 2)
            self.assertEqual(counter.increment("xlsx"), 1)
            self.assertEqual(counter.get("tianqi"), 2)

            reloaded = SkillCallCounter(path)
            self.assertEqual(reloaded.get("tianqi"), 2)
            self.assertEqual(reloaded.get("xlsx"), 1)

            reloaded.reset("tianqi")
            self.assertEqual(reloaded.get("tianqi"), 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("tianqi"), 0)
            self.assertEqual(data.get("xlsx"), 1)


class TestSkillSleepConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertFalse(get_skill_sleep_enabled({}))
        self.assertEqual(get_skill_sleep_call_threshold({}), 20)

    def test_reuses_evolution_enabled_and_signal_trigger(self) -> None:
        cfg = {
            "react": {
                "evolution": {
                    "enabled": True,
                    "signal_trigger": True,
                }
            }
        }
        self.assertTrue(get_skill_sleep_enabled(cfg))

        cfg["react"]["evolution"]["signal_trigger"] = False
        self.assertFalse(get_skill_sleep_enabled(cfg))


class TestSkillSleepRail(unittest.TestCase):
    def _ctx(
        self,
        *,
        tool_name: str,
        skill_name: str = "weather",
        failed: bool = False,
        directory_listing: bool = False,
        conversation_id: str = "sess-1",
        tool_args: dict | None = None,
    ) -> AgentCallbackContext:
        tool_call = MagicMock()
        args = tool_args if tool_args is not None else {"skill_name": skill_name}
        tool_call.arguments = args
        tool_msg = MagicMock()
        tool_msg.metadata = {
            "skill_name": skill_name,
            "is_directory_listing": directory_listing,
        }
        ctx = AgentCallbackContext(agent=MagicMock())
        inputs = ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args=args,
            tool_result="ok" if not failed else None,
            tool_msg=tool_msg,
        )
        inputs.conversation_id = conversation_id
        ctx.inputs = inputs
        ctx.session = MagicMock()
        ctx.extra = {}
        ctx.exception = RuntimeError("boom") if failed else None
        return ctx

    def _rail(self, tmp: str, threshold: int = 20) -> tuple[SkillCallCounter, MagicMock, SkillSleepRail]:
        state = Path(tmp)
        counter = SkillCallCounter(state / "call_counts.json")
        runner = MagicMock()
        runner.try_start.return_value = True
        rail = SkillSleepRail(
            counter=counter,
            runner=runner,
            call_threshold=threshold,
            last_skills_path=state / "last_skills.json",
        )
        return counter, runner, rail

    def test_load_alone_does_not_count(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, runner, rail = self._rail(tmp)

            async def _run() -> None:
                ctx = self._ctx(tool_name="skill_tool")
                await rail.before_invoke(ctx)
                await rail.after_tool_call(ctx)
                await rail.after_invoke(ctx)
                self.assertEqual(counter.get("weather"), 0)
                runner.try_start.assert_not_called()

            asyncio.run(_run())

    def test_one_usage_per_task_despite_multiple_work_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, runner, rail = self._rail(tmp)

            async def _run() -> None:
                ctx = self._ctx(tool_name="skill_tool")
                await rail.before_invoke(ctx)
                await rail.after_tool_call(ctx)
                with patch(
                    "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
                    "_resolve_active_skill_name",
                    return_value="",
                ):
                    await rail.after_tool_call(
                        self._ctx(tool_name="bash", tool_args={"command": "a"})
                    )
                    await rail.after_tool_call(
                        self._ctx(tool_name="bash", tool_args={"command": "b"})
                    )
                    await rail.after_tool_call(
                        self._ctx(tool_name="read_file", tool_args={"path": "x"})
                    )
                await rail.after_invoke(ctx)
                self.assertEqual(counter.get("weather"), 1)

            asyncio.run(_run())

    def test_second_task_counts_again(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, runner, rail = self._rail(tmp)

            async def _run() -> None:
                t1 = self._ctx(tool_name="skill_tool")
                await rail.before_invoke(t1)
                await rail.after_tool_call(t1)
                with patch(
                    "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
                    "_resolve_active_skill_name",
                    return_value="weather",
                ):
                    await rail.after_tool_call(
                        self._ctx(tool_name="bash", tool_args={"command": "1"})
                    )
                await rail.after_invoke(t1)
                self.assertEqual(counter.get("weather"), 1)

                t2 = self._ctx(tool_name="bash", tool_args={"command": "2"})
                await rail.before_invoke(t2)
                with patch(
                    "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
                    "_resolve_active_skill_name",
                    return_value="",
                ):
                    await rail.after_tool_call(t2)
                await rail.after_invoke(t2)
                self.assertEqual(counter.get("weather"), 2)

            asyncio.run(_run())

    def test_persisted_attribution_survives_rail_rebuild(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, _runner, rail = self._rail(tmp)

            async def _setup() -> None:
                ctx = self._ctx(tool_name="skill_tool")
                await rail.before_invoke(ctx)
                await rail.after_tool_call(ctx)
                await rail.after_invoke(ctx)

            asyncio.run(_setup())
            self.assertEqual(
                json.loads((Path(tmp) / "last_skills.json").read_text(encoding="utf-8")),
                {"sess-1": "weather"},
            )

            counter2 = SkillCallCounter(Path(tmp) / "call_counts.json")
            runner2 = MagicMock()
            rail2 = SkillSleepRail(
                counter=counter2,
                runner=runner2,
                call_threshold=20,
                last_skills_path=Path(tmp) / "last_skills.json",
            )

            async def _followup() -> None:
                t2 = self._ctx(tool_name="bash", tool_args={"command": "again"})
                await rail2.before_invoke(t2)
                with patch(
                    "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
                    "_resolve_active_skill_name",
                    return_value="",
                ):
                    await rail2.after_tool_call(t2)
                await rail2.after_invoke(t2)

            asyncio.run(_followup())
            self.assertEqual(counter2.get("weather"), 1)

    def test_skill_complete_clears_attribution(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, runner, rail = self._rail(tmp)

            async def _run() -> None:
                ctx = self._ctx(tool_name="skill_tool")
                await rail.before_invoke(ctx)
                await rail.after_tool_call(ctx)
                await rail.after_tool_call(self._ctx(tool_name="skill_complete"))
                await rail.after_invoke(ctx)

                t2 = self._ctx(tool_name="bash", tool_args={"command": "x"})
                await rail.before_invoke(t2)
                with patch(
                    "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
                    "_resolve_active_skill_name",
                    return_value="",
                ):
                    await rail.after_tool_call(t2)
                await rail.after_invoke(t2)
                self.assertEqual(counter.get("weather"), 0)

            asyncio.run(_run())

    def test_triggers_sleep_when_usage_exceeds_threshold(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, runner, rail = self._rail(tmp, threshold=1)

            async def _run() -> None:
                for i in range(2):
                    ctx = self._ctx(tool_name="skill_tool", conversation_id=f"s-{i}")
                    await rail.before_invoke(ctx)
                    await rail.after_tool_call(ctx)
                    with patch(
                        "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
                        "_resolve_active_skill_name",
                        return_value="weather",
                    ):
                        await rail.after_tool_call(
                            self._ctx(
                                tool_name="bash",
                                tool_args={"command": "x"},
                                conversation_id=f"s-{i}",
                            )
                        )
                    await rail.after_invoke(ctx)
                self.assertEqual(counter.get("weather"), 2)
                runner.try_start.assert_called_once_with("weather")

            asyncio.run(_run())


class TestSkillSleepRunner(unittest.TestCase):
    def test_try_start_resets_and_passes_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            traj = root / "trace"
            skills = root / "skills"
            state = root / "state"
            traj.mkdir()
            skills.mkdir()
            counter = SkillCallCounter(state / "call_counts.json")
            for _ in range(3):
                counter.increment("tianqi")

            tracked: list[asyncio.Task] = []

            def _track(task: asyncio.Task) -> None:
                tracked.append(task)

            runner = SkillSleepRunner(
                counter=counter,
                trajectory_dir=traj,
                skills_base_dir=skills,
                state_dir=state,
                backend="mock",
                on_task_created=_track,
            )

            async def _run() -> None:
                with patch(
                    "jiuwenswarm.agents.harness.common.skill_sleep.runner.run_sleep_cycle_sync"
                ) as mocked:
                    mocked.return_value = MagicMock(
                        night=1,
                        report=MagicMock(accepted=True, n_tasks=1),
                    )
                    started = runner.try_start("tianqi")
                    self.assertTrue(started)
                    self.assertEqual(counter.get("tianqi"), 0)
                    self.assertEqual(len(tracked), 1)
                    await tracked[0]
                    mocked.assert_called_once()
                    kwargs = mocked.call_args.kwargs
                    self.assertEqual(Path(kwargs["trajectory_dir"]), traj)
                    self.assertEqual(Path(kwargs["skills_base_dir"]), skills)
                    self.assertEqual(kwargs["skill_name"], "tianqi")
                    self.assertEqual(kwargs["backend"], "mock")

            asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
