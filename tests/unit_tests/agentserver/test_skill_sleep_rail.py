# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for skill sleep counter / rail / runner wiring."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY,
    BaseInterruptionState,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    RunContext,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.skill_sleep_rail import SkillSleepRail
from jiuwenswarm.agents.harness.common.skill_sleep.counter import SkillCallCounter
from jiuwenswarm.agents.harness.common.skill_sleep import runner as runner_mod
from jiuwenswarm.agents.harness.common.skill_sleep.runner import (
    SkillSleepRunner,
    SleepModelSpec,
)
from jiuwenswarm.common.config import (
    get_skill_sleep_action,
    get_skill_sleep_call_threshold,
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

            self.assertEqual(reloaded.reset("tianqi"), 2)
            self.assertEqual(reloaded.get("tianqi"), 0)
            self.assertEqual(reloaded.reset("tianqi"), 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("tianqi"), 0)
            self.assertEqual(data.get("xlsx"), 1)

            self.assertEqual(reloaded.add("tianqi", 2), 2)
            self.assertEqual(counter.get("tianqi"), 2)

    def test_cross_instance_increment_does_not_lose_updates(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "call_counts.json"
            a = SkillCallCounter(path)
            b = SkillCallCounter(path)
            self.assertEqual(a.increment("tianqi"), 1)
            self.assertEqual(b.increment("tianqi"), 2)
            self.assertEqual(a.increment("tianqi"), 3)
            self.assertEqual(a.get("tianqi"), 3)
            self.assertEqual(b.get("tianqi"), 3)


class TestSkillSleepConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(get_skill_sleep_call_threshold({}), 20)

    @staticmethod
    def _write_capabilities(skills_dir: Path, entries: list[dict]) -> None:
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "capabilities.json").write_text(
            json.dumps({"capabilities": entries}), encoding="utf-8"
        )

    def test_action_follows_registered_self_evolution(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"OFFICE_CLAW_CONFIG_ROOT": "", "OFFICE_CLAW_ROOT": ""}
        ):
            skills = Path(tmp) / "skills"
            self._write_capabilities(
                skills,
                [
                    {"type": "skill", "id": "a", "selfEvolution": "auto"},
                    {"type": "skill", "id": "s", "selfEvolution": "suggest"},
                    {"type": "skill", "id": "o", "selfEvolution": "off"},
                    {"type": "skill", "id": "b", "source": "builtin", "selfEvolution": "auto"},
                    {"type": "skill", "id": "n"},
                ],
            )
            self.assertEqual(get_skill_sleep_action("a", skills_dirs=skills), "auto")
            self.assertEqual(get_skill_sleep_action("s", skills_dirs=skills), "suggest")
            self.assertEqual(get_skill_sleep_action("o", skills_dirs=skills), "off")
            self.assertEqual(get_skill_sleep_action("b", skills_dirs=skills), "off")
            self.assertEqual(get_skill_sleep_action("n", skills_dirs=skills), "off")
            self.assertEqual(get_skill_sleep_action("tianqi", skills_dirs=skills), "off")
            self.assertEqual(get_skill_sleep_action("", skills_dirs=skills), "off")

    def test_action_off_without_capabilities_file(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"OFFICE_CLAW_CONFIG_ROOT": "", "OFFICE_CLAW_ROOT": ""}
        ):
            self.assertEqual(
                get_skill_sleep_action("tianqi", skills_dirs=Path(tmp) / "skills"), "off"
            )

    def test_action_off_on_resolve_error(self) -> None:
        with patch(
            "openjiuwen.agent_evolving.skill_self_evolution.get_skill_self_evolution_mode",
            side_effect=RuntimeError("boom"),
        ):
            self.assertEqual(get_skill_sleep_action("tianqi", skills_dirs="/x"), "off")


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

    def test_retrieval_and_lifecycle_tools_do_not_count(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, runner, rail = self._rail(tmp)

            async def _run() -> None:
                ctx = self._ctx(tool_name="skill_tool")
                await rail.before_invoke(ctx)
                await rail.after_tool_call(ctx)
                with patch(
                    "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
                    "_resolve_active_skill_name",
                    return_value="weather",
                ):
                    for tool in (
                        "search_skill",
                        "install_skill",
                        "uninstall_skill",
                        "skill_index_build",
                        "skill_branch_explore",
                        "skill_branch_peek",
                    ):
                        await rail.after_tool_call(
                            self._ctx(tool_name=tool, tool_args={"query": "x"})
                        )
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

                t2 = self._ctx(tool_name="skill_tool")
                await rail.before_invoke(t2)
                await rail.after_tool_call(t2)
                with patch(
                    "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
                    "_resolve_active_skill_name",
                    return_value="",
                ):
                    await rail.after_tool_call(
                        self._ctx(tool_name="bash", tool_args={"command": "2"})
                    )
                await rail.after_invoke(t2)
                self.assertEqual(counter.get("weather"), 2)

            asyncio.run(_run())

    def test_unfinished_skill_does_not_leak_into_next_task(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, _runner, rail = self._rail(tmp)

            async def _run() -> None:
                t1 = self._invoke_ctx()
                await rail.before_invoke(t1)
                await self._use_weather(rail, load=True)
                await rail.after_invoke(t1)
                self.assertEqual(counter.get("weather"), 1)

                for _ in range(3):
                    t = self._invoke_ctx()
                    await rail.before_invoke(t)
                    with patch(
                        "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
                        "_resolve_active_skill_name",
                        return_value="",
                    ):
                        await rail.after_tool_call(
                            self._ctx(tool_name="bash", tool_args={"command": "x"})
                        )
                    await rail.after_invoke(t)
                self.assertEqual(counter.get("weather"), 1)

            asyncio.run(_run())
            self.assertEqual(self._last_skills_file(tmp), {})

    def _rebuild(self, tmp: str) -> tuple[SkillCallCounter, SkillSleepRail]:
        counter = SkillCallCounter(Path(tmp) / "call_counts.json")
        rail = SkillSleepRail(
            counter=counter,
            runner=MagicMock(),
            call_threshold=20,
            last_skills_path=Path(tmp) / "last_skills.json",
        )
        return counter, rail

    async def _load_then_pause(self, rail: SkillSleepRail) -> None:
        await rail.before_invoke(self._invoke_ctx())
        await rail.after_tool_call(self._ctx(tool_name="skill_tool"))
        await rail.after_invoke(self._invoke_ctx(paused=True))

    async def _unattributed_work(self, rail: SkillSleepRail, invoke) -> None:
        await rail.before_invoke(invoke)
        with patch(
            "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
            "_resolve_active_skill_name",
            return_value="",
        ):
            await rail.after_tool_call(
                self._ctx(tool_name="bash", tool_args={"command": "again"})
            )
        await rail.after_invoke(invoke)

    def test_persisted_attribution_survives_rail_rebuild_on_resume(self) -> None:
        with TemporaryDirectory() as tmp:
            _counter, _runner, rail = self._rail(tmp)
            asyncio.run(self._load_then_pause(rail))
            self.assertEqual(self._last_skills_file(tmp), {"sess-1": "weather"})

            counter2, rail2 = self._rebuild(tmp)
            asyncio.run(
                self._unattributed_work(
                    rail2, self._invoke_ctx(source="permission_interrupt")
                )
            )
            self.assertEqual(counter2.get("weather"), 1)

    def test_persisted_attribution_dropped_on_new_task_after_rebuild(self) -> None:
        with TemporaryDirectory() as tmp:
            _counter, _runner, rail = self._rail(tmp)
            asyncio.run(self._load_then_pause(rail))

            counter2, rail2 = self._rebuild(tmp)
            asyncio.run(self._unattributed_work(rail2, self._invoke_ctx()))
            self.assertEqual(counter2.get("weather"), 0)

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
                runner.try_start.assert_called_once_with("weather", session_id="s-1")

            asyncio.run(_run())

    @staticmethod
    def _invoke_ctx(
        *,
        source: str = "",
        paused: bool = False,
        result: dict | None = None,
        conversation_id: str = "sess-1",
    ) -> AgentCallbackContext:
        """Outer DeepAgent invoke ctx: fresh empty ``extra`` like the framework."""
        run_context = RunContext(
            extra={"__jiuwenswarm_chat_send_source__": source} if source else {}
        )
        ctx = AgentCallbackContext(agent=MagicMock())
        ctx.inputs = InvokeInputs(
            query="q",
            conversation_id=conversation_id,
            result=result,
            run_context=run_context,
        )
        pending = MagicMock(spec=BaseInterruptionState) if paused else None
        session = MagicMock()
        session.get_state.side_effect = (
            lambda key: pending if key == INTERRUPTION_KEY else None
        )
        ctx.session = session
        return ctx

    async def _use_weather(self, rail: SkillSleepRail, *, load: bool) -> None:
        if load:
            await rail.after_tool_call(self._ctx(tool_name="skill_tool"))
        with patch(
            "jiuwenswarm.agents.harness.common.rails.skill_sleep_rail."
            "_resolve_active_skill_name",
            return_value="weather",
        ):
            await rail.after_tool_call(
                self._ctx(tool_name="bash", tool_args={"command": "x"})
            )

    def test_usage_before_hitl_pause_is_counted_after_resume(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, _runner, rail = self._rail(tmp)

            async def _run() -> None:
                t1 = self._invoke_ctx()
                await rail.before_invoke(t1)
                await self._use_weather(rail, load=True)
                await rail.after_invoke(self._invoke_ctx(paused=True))
                self.assertEqual(counter.get("weather"), 0)

                resume = self._invoke_ctx(source="permission_interrupt")
                await rail.before_invoke(resume)
                await rail.after_invoke(resume)
                self.assertEqual(counter.get("weather"), 1)

            asyncio.run(_run())

    def test_usage_across_hitl_pause_counts_once(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, _runner, rail = self._rail(tmp)

            async def _run() -> None:
                await rail.before_invoke(self._invoke_ctx())
                await self._use_weather(rail, load=True)
                await rail.after_invoke(self._invoke_ctx(paused=True))

                for source in ("ask_user_interrupt", "confirm_interrupt"):
                    await rail.before_invoke(self._invoke_ctx(source=source))
                    await self._use_weather(rail, load=False)
                    await rail.after_invoke(self._invoke_ctx(paused=True))
                self.assertEqual(counter.get("weather"), 0)

                final = self._invoke_ctx(source="permission_interrupt")
                await rail.before_invoke(final)
                await self._use_weather(rail, load=False)
                await rail.after_invoke(final)
                self.assertEqual(counter.get("weather"), 1)

            asyncio.run(_run())

    def test_interrupt_result_type_pauses_flush(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, _runner, rail = self._rail(tmp)

            async def _run() -> None:
                await rail.before_invoke(self._invoke_ctx())
                await self._use_weather(rail, load=True)
                await rail.after_invoke(
                    self._invoke_ctx(result={"result_type": "interrupt"})
                )
                self.assertEqual(counter.get("weather"), 0)

            asyncio.run(_run())

    def test_abandoned_hitl_task_is_counted_on_next_new_task(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, _runner, rail = self._rail(tmp)

            async def _run() -> None:
                await rail.before_invoke(self._invoke_ctx())
                await self._use_weather(rail, load=True)
                await rail.after_invoke(self._invoke_ctx(paused=True))
                self.assertEqual(counter.get("weather"), 0)

                await rail.before_invoke(self._invoke_ctx())
                self.assertEqual(counter.get("weather"), 1)

            asyncio.run(_run())

    @staticmethod
    def _last_skills_file(tmp: str) -> dict[str, str]:
        raw = json.loads((Path(tmp) / "last_skills.json").read_text(encoding="utf-8"))
        return {sid: entry["skill"] for sid, entry in raw.items()}

    def test_last_skills_prune_by_ttl(self) -> None:
        with TemporaryDirectory() as tmp:
            _counter, _runner, rail = self._rail(tmp)
            rail._remember_skill("old", "weather")
            rail._remember_skill("new", "xlsx")
            rail._last_skill_ts["old"] = time.time() - (8 * 24 * 3600)
            rail._persist_last_skills()
            self.assertEqual(self._last_skills_file(tmp), {"new": "xlsx"})
            self.assertNotIn("old", rail._last_skill)

    def test_legacy_last_skills_format_is_loaded(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "last_skills.json").write_text(
                json.dumps({"sess-1": "weather"}), encoding="utf-8"
            )
            _counter, _runner, rail = self._rail(tmp)
            self.assertEqual(rail._last_skill, {"sess-1": "weather"})

    def test_rails_sharing_last_skills_file_do_not_clobber(self) -> None:
        with TemporaryDirectory() as tmp:
            _c1, _r1, rail_a = self._rail(tmp)
            _c2, _r2, rail_b = self._rail(tmp)
            rail_a._remember_skill("sess-a", "weather")
            rail_b._remember_skill("sess-b", "xlsx")
            rail_a._persist_last_skills()
            rail_b._persist_last_skills()
            self.assertEqual(
                self._last_skills_file(tmp), {"sess-a": "weather", "sess-b": "xlsx"}
            )

            rail_a.forget_session("sess-a")
            self.assertEqual(self._last_skills_file(tmp), {"sess-b": "xlsx"})

    def test_forget_session_counts_paused_task_and_drops_attribution(self) -> None:
        with TemporaryDirectory() as tmp:
            counter, _runner, rail = self._rail(tmp)

            async def _run() -> None:
                await rail.before_invoke(self._invoke_ctx())
                await self._use_weather(rail, load=True)
                await rail.after_invoke(self._invoke_ctx(paused=True))

            asyncio.run(_run())
            self.assertEqual(counter.get("weather"), 0)
            self.assertEqual(self._last_skills_file(tmp), {"sess-1": "weather"})

            rail.forget_session("sess-1")
            self.assertEqual(counter.get("weather"), 1)
            self.assertNotIn("sess-1", rail._last_skill)
            self.assertEqual(self._last_skills_file(tmp), {})


class TestSkillSleepRunner(unittest.TestCase):
    def setUp(self) -> None:
        action_patch = patch.object(runner_mod, "get_skill_sleep_action", return_value="auto")
        self.action_mock = action_patch.start()
        self.addCleanup(action_patch.stop)

    def test_off_action_resets_count_and_does_not_start(self) -> None:
        self.action_mock.return_value = "off"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked: list[asyncio.Task] = []
            counter, runner = self._runner(root, tracked=tracked)
            for _ in range(3):
                counter.increment("tianqi")

            async def _run() -> None:
                with patch.object(runner_mod, "run_sleep_cycle_sync") as mocked:
                    self.assertFalse(runner.try_start("tianqi"))
                    mocked.assert_not_called()
                self.assertEqual(tracked, [])
                self.assertEqual(counter.get("tianqi"), 0)
                self.assertFalse(runner.inflight)

            asyncio.run(_run())
        self.action_mock.assert_called_once_with("tianqi", skills_dirs=[root / "skills"])

    def test_skills_dirs_provider_is_resolved_per_start(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            extra = root / ".office-claw" / "skills"
            tracked: list[asyncio.Task] = []
            (root / "trace").mkdir()
            counter = SkillCallCounter(root / "state" / "call_counts.json")
            counter.increment("tianqi")
            runner = SkillSleepRunner(
                counter=counter,
                trajectory_dir=root / "trace",
                skills_base_dir=root / "skills",
                state_dir=root / "state",
                backend="mock",
                on_task_created=tracked.append,
                skills_dirs_provider=lambda: [root / "skills", extra, root / "skills"],
            )

            async def _run() -> MagicMock:
                with patch.object(runner_mod, "run_sleep_cycle_sync") as mocked:
                    mocked.return_value = MagicMock(night=1, report=MagicMock())
                    self.assertTrue(runner.try_start("tianqi"))
                    await tracked[0]
                    return mocked

            mocked = asyncio.run(_run())
        expected = [root / "skills", extra]
        self.assertEqual(mocked.call_args.kwargs["skills_base_dir"], expected)
        self.action_mock.assert_called_once_with("tianqi", skills_dirs=expected)

    def test_skills_dirs_provider_failure_falls_back_to_initial(self) -> None:
        def _boom() -> list[Path]:
            raise RuntimeError("config broken")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trace").mkdir()
            runner = SkillSleepRunner(
                counter=SkillCallCounter(root / "state" / "call_counts.json"),
                trajectory_dir=root / "trace",
                skills_base_dir=root / "skills",
                state_dir=root / "state",
                backend="mock",
                skills_dirs_provider=_boom,
            )
            self.assertEqual(runner._current_skills_dirs(), [root / "skills"])

    def test_resolved_action_is_passed_to_sleep_cycle(self) -> None:
        self.action_mock.return_value = "suggest"
        with TemporaryDirectory() as tmp:
            tracked: list[asyncio.Task] = []
            counter, runner = self._runner(Path(tmp), tracked=tracked)
            counter.increment("tianqi")

            async def _run() -> MagicMock:
                with patch.object(runner_mod, "run_sleep_cycle_sync") as mocked:
                    mocked.return_value = MagicMock(night=1, report=MagicMock())
                    self.assertTrue(runner.try_start("tianqi"))
                    await tracked[0]
                    return mocked

            mocked = asyncio.run(_run())
        self.assertEqual(mocked.call_args.kwargs["action"], "suggest")

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
                    self.assertEqual(kwargs["skills_base_dir"], [skills])
                    self.assertEqual(kwargs["skill_name"], "tianqi")
                    self.assertEqual(kwargs["backend"], "mock")
                    self.assertFalse(runner.inflight)

            asyncio.run(_run())

    def test_create_task_failure_restores_inflight_and_keeps_counter(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            (root / "trace").mkdir()
            counter = SkillCallCounter(state / "call_counts.json")
            for _ in range(3):
                counter.increment("tianqi")

            runner = SkillSleepRunner(
                counter=counter,
                trajectory_dir=root / "trace",
                skills_base_dir=root / "skills",
                state_dir=state,
                backend="mock",
            )

            async def _run() -> None:
                loop = asyncio.get_running_loop()

                def _boom(*_a, **_k):
                    raise RuntimeError("create_task failed")

                with patch.object(loop, "create_task", side_effect=_boom):
                    started = runner.try_start("tianqi")
                self.assertFalse(started)
                self.assertFalse(runner.inflight)
                self.assertEqual(counter.get("tianqi"), 3)
                # Can start again after a failed schedule.
                with patch(
                    "jiuwenswarm.agents.harness.common.skill_sleep.runner.run_sleep_cycle_sync"
                ) as mocked:
                    mocked.return_value = MagicMock(
                        night=1,
                        report=MagicMock(accepted=True, n_tasks=1),
                    )
                    self.assertTrue(runner.try_start("tianqi"))
                    await asyncio.sleep(0)
                    while runner.inflight:
                        await asyncio.sleep(0.01)

            asyncio.run(_run())

    def _runner(
        self, root: Path, *, state: str = "state", tracked: list | None = None
    ) -> tuple[SkillCallCounter, SkillSleepRunner]:
        (root / "trace").mkdir(exist_ok=True)
        counter = SkillCallCounter(root / state / "call_counts.json")
        runner = SkillSleepRunner(
            counter=counter,
            trajectory_dir=root / "trace",
            skills_base_dir=root / "skills",
            state_dir=root / state,
            backend="mock",
            on_task_created=tracked.append if tracked is not None else None,
        )
        return counter, runner

    def test_sleep_failure_restores_count_and_backs_off(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked: list[asyncio.Task] = []
            counter, runner = self._runner(root, tracked=tracked)
            for _ in range(3):
                counter.increment("tianqi")

            async def _run() -> None:
                with patch.object(
                    runner_mod,
                    "run_sleep_cycle_sync",
                    side_effect=ValueError("Missing required environment variables"),
                ) as mocked:
                    self.assertTrue(runner.try_start("tianqi"))
                    self.assertEqual(counter.get("tianqi"), 0)
                    counter.increment("tianqi")
                    await tracked[0]
                    self.assertEqual(counter.get("tianqi"), 4)
                    self.assertFalse(runner.inflight)

                    self.assertFalse(runner.try_start("tianqi"))
                    mocked.assert_called_once()
                    self.assertEqual(counter.get("tianqi"), 4)

            with self.assertLogs(runner_mod.logger, level="ERROR"):
                asyncio.run(_run())
            ledger = json.loads(
                (root / "state" / "skill_sleep_failures.json").read_text(encoding="utf-8")
            )
            self.assertEqual(ledger["tianqi"]["failures"], 1)
            self.assertGreater(ledger["tianqi"]["next_retry_at"], time.time())

    def test_success_after_backoff_clears_failures(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked: list[asyncio.Task] = []
            counter, runner = self._runner(root, tracked=tracked)
            ledger_path = root / "state" / "skill_sleep_failures.json"
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_path.write_text(
                json.dumps({"tianqi": {"failures": 2, "next_retry_at": time.time() - 1}}),
                encoding="utf-8",
            )

            async def _run() -> None:
                with patch.object(runner_mod, "run_sleep_cycle_sync") as mocked:
                    mocked.return_value = MagicMock(night=1, report=MagicMock())
                    self.assertTrue(runner.try_start("tianqi"))
                    await tracked[0]

            asyncio.run(_run())
            self.assertEqual(
                json.loads(ledger_path.read_text(encoding="utf-8")), {}
            )

    def test_preflight_failure_keeps_count_and_does_not_start(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = SkillCallCounter(root / "state" / "call_counts.json")
            counter.increment("tianqi")
            runner = SkillSleepRunner(
                counter=counter,
                trajectory_dir=root / "missing-trace",
                skills_base_dir=root / "skills",
                state_dir=root / "state",
                backend="mock",
            )

            async def _run() -> None:
                with patch.object(runner_mod, "run_sleep_cycle_sync") as mocked:
                    self.assertFalse(runner.try_start("tianqi"))
                    mocked.assert_not_called()

            with self.assertLogs(runner_mod.logger, level="ERROR") as logs:
                asyncio.run(_run())
            self.assertIn("trajectory dir not found", "\n".join(logs.output))
            self.assertEqual(counter.get("tianqi"), 1)
            self.assertFalse(runner.inflight)

    def test_preflight_rejects_env_fallback_without_env(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trace").mkdir()
            runner = SkillSleepRunner(
                counter=SkillCallCounter(root / "state" / "call_counts.json"),
                trajectory_dir=root / "trace",
                skills_base_dir=root / "skills",
                state_dir=root / "state",
                backend="model",
            )
            with patch.object(
                runner_mod, "_env_model_settings", return_value=({}, ["API_KEY"])
            ):
                self.assertIn("API_KEY", runner._preflight(None))
                spec = SleepModelSpec(client_config={}, request_config={}, model_name="m")
                self.assertEqual(runner._preflight(spec), "")

    def test_runners_sharing_skills_dir_are_mutually_exclusive(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked: list[asyncio.Task] = []
            _c1, first = self._runner(root, state="state-a", tracked=tracked)
            _c2, second = self._runner(root, state="state-b")
            release = threading.Event()

            def _slow_cycle(**_kwargs):
                release.wait(5)
                return MagicMock(night=1, report=MagicMock())

            async def _run() -> None:
                with patch.object(
                    runner_mod, "run_sleep_cycle_sync", side_effect=_slow_cycle
                ):
                    self.assertTrue(first.try_start("tianqi"))
                    self.assertFalse(second.try_start("tianqi"))
                    release.set()
                    await tracked[0]
                    self.assertTrue(second.try_start("tianqi"))
                    while second.inflight:
                        await asyncio.sleep(0.01)

            asyncio.run(_run())

    def _run_publish(
        self,
        *,
        adopted: list,
        session_id: str = "sess-1",
        on_published=None,
    ) -> AsyncMock:
        callback = on_published or AsyncMock()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trace").mkdir()
            counter = SkillCallCounter(root / "state" / "call_counts.json")
            counter.increment("tianqi")
            tracked: list[asyncio.Task] = []
            runner = SkillSleepRunner(
                counter=counter,
                trajectory_dir=root / "trace",
                skills_base_dir=root / "skills",
                state_dir=root / "state",
                backend="mock",
                on_task_created=tracked.append,
                on_published=callback,
            )

            async def _run() -> None:
                with patch.object(runner_mod, "run_sleep_cycle_sync") as mocked:
                    mocked.return_value = SimpleNamespace(
                        night=6, report=MagicMock(), adopted_skills=adopted
                    )
                    self.assertTrue(runner.try_start("tianqi", session_id=session_id))
                    await tracked[0]

            asyncio.run(_run())
            ledger = root / "state" / "skill_sleep_failures.json"
            if ledger.exists():
                self.assertEqual(json.loads(ledger.read_text(encoding="utf-8")), {})
        return callback

    @staticmethod
    def _adopted(new: str, previous: str = "1.0.0") -> SimpleNamespace:
        return SimpleNamespace(
            skill_name="tianqi", previous_version=previous, new_version=new
        )

    def test_auto_adopt_pushes_published(self) -> None:
        callback = self._run_publish(adopted=[self._adopted("1.1.0")])
        callback.assert_awaited_once_with(
            skill_name="tianqi",
            version="1.1.0",
            session_id="sess-1",
            request_id="skill-sleep-6",
        )

    def test_no_adoption_or_unchanged_version_skips_published(self) -> None:
        self._run_publish(adopted=[]).assert_not_awaited()
        self._run_publish(adopted=[self._adopted("1.0.0")]).assert_not_awaited()
        self._run_publish(adopted=[self._adopted("")]).assert_not_awaited()

    def test_missing_session_skips_published(self) -> None:
        with self.assertLogs(runner_mod.logger, level="WARNING") as logs:
            callback = self._run_publish(adopted=[self._adopted("1.1.0")], session_id="")
        callback.assert_not_awaited()
        self.assertIn("no_session_context", "\n".join(logs.output))

    def test_published_push_failure_does_not_fail_cycle(self) -> None:
        callback = AsyncMock(side_effect=RuntimeError("gateway down"))
        with self.assertLogs(runner_mod.logger, level="WARNING") as logs:
            self._run_publish(adopted=[self._adopted("1.1.0")], on_published=callback)
        callback.assert_awaited_once()
        self.assertIn("published push failed", "\n".join(logs.output))

    def _run_suggest_notify(
        self,
        *,
        edits: list | None = None,
        suggest_written: int | None = 1,
        session_id: str = "sess-1",
        on_suggest=None,
    ) -> AsyncMock:
        from openjiuwen.agent_evolving.skill_train.sleep.types import EditRecord

        callback = on_suggest or AsyncMock()
        self.action_mock.return_value = "suggest"
        if edits is None:
            edits = [EditRecord(target="skill", op="add", content="use format=j1")]
        outcome = SimpleNamespace(
            night=7,
            report=SimpleNamespace(n_tasks=2, edits=edits, rejected_edits=[]),
            adopted_skills=[],
        )
        cycle_result = (
            (outcome, suggest_written) if suggest_written is not None else outcome
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trace").mkdir()
            counter = SkillCallCounter(root / "state" / "call_counts.json")
            counter.increment("tianqi")
            tracked: list[asyncio.Task] = []
            runner = SkillSleepRunner(
                counter=counter,
                trajectory_dir=root / "trace",
                skills_base_dir=root / "skills",
                state_dir=root / "state",
                backend="mock",
                on_task_created=tracked.append,
                on_suggest=callback,
            )

            async def _run() -> None:
                with patch.object(runner_mod, "run_sleep_cycle_sync") as mocked:
                    mocked.return_value = cycle_result
                    self.assertTrue(runner.try_start("tianqi", session_id=session_id))
                    await tracked[0]

            asyncio.run(_run())
        return callback

    def test_suggest_pushes_status_notify(self) -> None:
        callback = self._run_suggest_notify()
        callback.assert_awaited_once_with(
            skill_name="tianqi",
            session_id="sess-1",
            request_id="skill-sleep-7",
        )

    def test_suggest_zero_written_skips_notify(self) -> None:
        self._run_suggest_notify(edits=[], suggest_written=0).assert_not_awaited()

    def test_suggest_missing_session_skips_notify(self) -> None:
        with self.assertLogs(runner_mod.logger, level="WARNING") as logs:
            callback = self._run_suggest_notify(session_id="")
        callback.assert_not_awaited()
        self.assertIn("no_session_context", "\n".join(logs.output))

    def test_suggest_push_failure_does_not_fail_cycle(self) -> None:
        callback = AsyncMock(side_effect=RuntimeError("gateway down"))
        with self.assertLogs(runner_mod.logger, level="WARNING") as logs:
            self._run_suggest_notify(on_suggest=callback)
        callback.assert_awaited_once()
        self.assertIn("suggest push failed", "\n".join(logs.output))

    def _run_with_provider(self, provider) -> MagicMock:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            (root / "trace").mkdir()
            counter = SkillCallCounter(state / "call_counts.json")
            tracked: list[asyncio.Task] = []
            runner = SkillSleepRunner(
                counter=counter,
                trajectory_dir=root / "trace",
                skills_base_dir=root / "skills",
                state_dir=state,
                backend="model",
                on_task_created=tracked.append,
                model_provider=provider,
            )

            async def _run() -> MagicMock:
                with patch(
                    "jiuwenswarm.agents.harness.common.skill_sleep.runner.run_sleep_cycle_sync"
                ) as mocked, patch.object(
                    runner_mod, "_env_model_settings", return_value=({}, [])
                ):
                    mocked.return_value = MagicMock(night=1, report=MagicMock())
                    self.assertTrue(runner.try_start("tianqi"))
                    await tracked[0]
                    return mocked

            return asyncio.run(_run())

    def test_model_provider_spec_is_passed_to_sleep_cycle(self) -> None:
        client_cfg = {"api_key": "k", "api_base": "http://main"}
        request_cfg = {"model": "main-model"}
        spec = SleepModelSpec(
            client_config=client_cfg,
            request_config=request_cfg,
            model_name="main-model",
        )
        mocked = self._run_with_provider(lambda: spec)
        passed = mocked.call_args.kwargs["model_spec"]
        self.assertIsInstance(passed, SleepModelSpec)
        self.assertEqual(passed.model_name, "main-model")
        self.assertEqual(passed.client_config, client_cfg)
        self.assertEqual(passed.request_config, request_cfg)
        # Snapshot is detached from adapter-owned configs.
        self.assertIsNot(passed.client_config, client_cfg)

    def test_model_provider_failure_falls_back_to_env(self) -> None:
        def _boom() -> SleepModelSpec:
            raise RuntimeError("no model")

        mocked = self._run_with_provider(_boom)
        self.assertIsNone(mocked.call_args.kwargs["model_spec"])

        mocked = self._run_with_provider(lambda: None)
        self.assertIsNone(mocked.call_args.kwargs["model_spec"])


class TestRunSleepCycleSyncAction(unittest.TestCase):
    _CYCLE = "openjiuwen.agent_evolving.skill_train.sleep.run_sleep_cycle"
    _STORE = "openjiuwen.agent_evolving.checkpointing.evolution_store.EvolutionStore"

    def _run(self, action: str, outcome: object) -> tuple[MagicMock, MagicMock, list]:
        seed = [SimpleNamespace(skill_hint="tianqi")]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trace").mkdir()
            with patch(self._CYCLE, return_value=outcome) as cycle, patch(
                self._STORE
            ) as store_cls, patch.object(
                runner_mod, "_mine_skill_tasks", return_value=seed
            ) as mine_tasks:
                store = store_cls.return_value

                async def _append(*_a, **_k) -> None:
                    return None

                store.append_record = MagicMock(side_effect=_append)
                runner_mod.run_sleep_cycle_sync(
                    trajectory_dir=root / "trace",
                    skills_base_dir=root / "skills",
                    state_dir=root / "state",
                    skill_name="tianqi",
                    backend="mock",
                    action=action,
                )
        mine_tasks.assert_called_once()
        self.assertEqual(mine_tasks.call_args.args[1], "tianqi")
        self.assertIs(cycle.call_args.kwargs["seed_tasks"], seed)
        return cycle, store.append_record, seed

    @staticmethod
    def _make_outcome(edits: list) -> SimpleNamespace:
        return SimpleNamespace(night=3, report=SimpleNamespace(n_tasks=5, edits=edits))

    def test_auto_adopts_and_writes_no_suggestions(self) -> None:
        from openjiuwen.agent_evolving.skill_train.sleep.types import EditRecord

        outcome = self._make_outcome([EditRecord(target="skill", op="add", content="tip")])
        cycle, append_record, _ = self._run("auto", outcome)
        self.assertFalse(cycle.call_args.kwargs["dry_run"])
        append_record.assert_not_called()

    def test_suggest_is_dry_run_and_appends_experiences(self) -> None:
        from openjiuwen.agent_evolving.skill_train.sleep.types import EditRecord

        outcome = self._make_outcome(
            [
                EditRecord(target="skill", op="add", content="use format=j1", rationale="uv"),
                EditRecord(target="skill", op="replace", content="new line", anchor="old"),
                EditRecord(target="skill", op="delete", anchor="stale"),
                EditRecord(target="skill", op="add", content="   "),
            ]
        )
        cycle, append_record, _ = self._run("suggest", outcome)
        self.assertTrue(cycle.call_args.kwargs["dry_run"])
        self.assertEqual(append_record.call_count, 2)
        for call in append_record.call_args_list:
            self.assertEqual(call.args[0], "tianqi")
            self.assertFalse(call.kwargs["update_skill_md"])
            record = call.args[1]
            self.assertEqual(record.review_status, "suggest")
            self.assertEqual(record.source, "skill_sleep")
            self.assertEqual(record.change.action, "append")
            self.assertEqual(record.change.section, "Instructions")
        first = append_record.call_args_list[0].args[1]
        self.assertEqual(first.change.content, "use format=j1")
        self.assertEqual(first.root_cause, "uv")
        self.assertIn("night=3", first.context)
        self.assertIn("gate=accepted", first.context)

    def test_suggest_also_records_gate_rejected_edits(self) -> None:
        from openjiuwen.agent_evolving.skill_train.sleep.types import EditRecord

        outcome = SimpleNamespace(
            night=4,
            report=SimpleNamespace(
                n_tasks=6,
                edits=[EditRecord(target="skill", op="add", content="kept")],
                rejected_edits=[
                    EditRecord(target="skill", op="add", content="use %u for UV", rationale="uv"),
                    EditRecord(target="skill", op="add", content="kept"),
                    EditRecord(target="skill", op="delete", anchor="old"),
                ],
            ),
        )
        _, append_record, _ = self._run("suggest", outcome)
        records = [call.args[1] for call in append_record.call_args_list]
        self.assertEqual([r.change.content for r in records], ["kept", "use %u for UV"])
        self.assertIn("gate=accepted", records[0].context)
        self.assertIn("gate=rejected", records[1].context)
        self.assertEqual(records[1].review_status, "suggest")

    def test_all_skills_dirs_reach_evolution_store(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trace").mkdir()
            first, second = root / "office-claw-skills", root / ".office-claw" / "skills"
            with patch(self._CYCLE) as cycle, patch(self._STORE) as store_cls, patch.object(
                runner_mod, "_mine_skill_tasks", return_value=[]
            ):
                runner_mod.run_sleep_cycle_sync(
                    trajectory_dir=root / "trace",
                    skills_base_dir=[first, second, first],
                    state_dir=root / "state",
                    skill_name="tianqi",
                    backend="mock",
                )
        store_cls.assert_called_once_with([str(first), str(second)])
        self.assertEqual(
            cycle.call_args.args[0].skills_base_dir, f"{first};{second}"
        )
        self.assertIs(cycle.call_args.kwargs["evolution_store"], store_cls.return_value)

    def test_mine_skill_tasks_keeps_only_hinted_skill(self) -> None:
        tasks = [
            SimpleNamespace(skill_hint="tianqi"),
            SimpleNamespace(skill_hint="other"),
            SimpleNamespace(skill_hint=""),
            SimpleNamespace(skill_hint=" tianqi "),
        ]
        cfg = SimpleNamespace(
            max_tasks_per_night=40, val_fraction=0.34, test_fraction=0.0, seed=42
        )
        with patch(
            "openjiuwen.agent_evolving.skill_train.sleep.harvest.harvest_otlp_trajectories",
            return_value=["digest"],
        ), patch(
            "openjiuwen.agent_evolving.skill_train.sleep.mine.mine", return_value=tasks
        ) as mine:
            kept = runner_mod._mine_skill_tasks(cfg, "tianqi")
        mine.assert_called_once()
        self.assertEqual(kept, [tasks[0], tasks[3]])


class TestBuildChatClient(unittest.TestCase):
    def test_spec_path_does_not_read_env(self) -> None:
        from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig

        spec = SleepModelSpec(
            client_config=ModelClientConfig(
                client_provider="OpenAI", api_key="k", api_base="http://main"
            ),
            request_config=ModelRequestConfig(model="main-model"),
            model_name="main-model",
        )
        with patch.object(runner_mod, "_build_env_model") as env_model, patch.object(
            runner_mod, "_load_dotenv"
        ) as load_dotenv:
            client = runner_mod._build_chat_client(spec)
        env_model.assert_not_called()
        load_dotenv.assert_not_called()
        self.assertEqual(client.model, "main-model")
        self.assertEqual(client.llm.model_client_config.api_base, "http://main")

    def test_sleep_client_does_not_share_http_client_across_loops(self) -> None:
        from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig

        main_cfg = ModelClientConfig(
            client_provider="OpenAI", api_key="k", api_base="http://main"
        )
        spec = SleepModelSpec(
            client_config=main_cfg,
            request_config=ModelRequestConfig(model="main-model"),
            model_name="main-model",
        )
        client = runner_mod._build_chat_client(spec)
        self.assertFalse(client.llm.model_client_config.use_shared_llm_http_client)
        self.assertTrue(main_cfg.use_shared_llm_http_client)

    def test_reflect_reply_fenced_array_is_unwrapped(self) -> None:
        from openjiuwen.agent_evolving.skill_train.sleep.model_backend import (
            _edits_from_payload,
        )

        fence = "`" * 3
        array = '[{"op":"add","content":"UV 用 format=j1","rationale":"r"}]'
        inner = MagicMock()
        inner.chat.return_value = (f"好的：\n{fence}json\n{array}\n{fence}", {"m": 1})
        client = runner_mod._ReflectNormalizingChatClient(inner)

        text, meta = client.chat(system="s", user="u", stage="sleep_reflect")
        self.assertEqual(text, array)
        self.assertEqual(meta, {"m": 1})
        self.assertEqual(len(_edits_from_payload(text, budget=4, target="skill")), 1)

    def test_non_reflect_stage_and_plain_reply_pass_through(self) -> None:
        fence = "`" * 3
        fenced = f'{fence}json\n{{"score": 1}}\n{fence}'
        inner = MagicMock()
        inner.chat.return_value = (fenced, {})
        inner.model = "m"
        client = runner_mod._ReflectNormalizingChatClient(inner)
        self.assertEqual(client.chat(system="s", user="u", stage="sleep_judge")[0], fenced)
        self.assertEqual(client.model, "m")

        inner.chat.return_value = ("[]", {})
        self.assertEqual(client.chat(system="s", user="u", stage="sleep_reflect")[0], "[]")
        inner.chat.return_value = (f"{fence}\nnot json\n{fence}", {})
        self.assertEqual(
            client.chat(system="s", user="u", stage="sleep_reflect")[0],
            f"{fence}\nnot json\n{fence}",
        )

    def test_body_anchored_replace_becomes_add_and_delete_is_dropped(self) -> None:
        from openjiuwen.agent_evolving.skill_train.sleep.memory import (
            apply_edits_detailed,
        )
        from openjiuwen.agent_evolving.skill_train.sleep.model_backend import (
            _edits_from_payload,
        )

        skill = "# 天气\n\n## 格式代码\n\n- `%p` — 降水量\n- `%l` — 地点\n"
        reply = json.dumps(
            [
                {
                    "op": "replace",
                    "anchor": "- `%p` — 降水量",
                    "content": "- `%p` — 降水量\n- `%u` — UV指数",
                    "rationale": "uv",
                },
                {"op": "delete", "anchor": "- `%l` — 地点"},
                {"op": "replace", "anchor": "- `%p` — 降水量", "content": "- `%p` — 降水量"},
                {"op": "add", "content": "缺数据时重新查询"},
            ],
            ensure_ascii=False,
        )
        inner = MagicMock()
        inner.chat.return_value = (reply, {})
        client = runner_mod._ReflectNormalizingChatClient(inner)
        text, _ = client.chat(system="s", user=f"# Current skill\n{skill}", stage="sleep_reflect")

        edits = _edits_from_payload(text, budget=4, target="skill")
        self.assertEqual([(e.op, e.content, e.anchor) for e in edits], [
            ("add", "- `%u` — UV指数", ""),
            ("add", "缺数据时重新查询", ""),
        ])
        self.assertEqual(edits[0].rationale, "uv")
        new_doc, applied, unmatched = apply_edits_detailed(skill, edits)
        self.assertEqual((len(applied), len(unmatched)), (2, 0))
        self.assertIn("`%u` — UV指数", new_doc)
        self.assertIn("- `%l` — 地点", new_doc)

    def test_replace_inside_learned_region_is_kept(self) -> None:
        from openjiuwen.agent_evolving.skill_train.sleep.memory import (
            LEARNED_END,
            LEARNED_START,
        )

        skill = f"# 天气\n{LEARNED_START}\n- 旧规则\n{LEARNED_END}\n"
        reply = json.dumps(
            {"edits": [{"op": "replace", "anchor": "旧规则", "content": "新规则"}]},
            ensure_ascii=False,
        )
        out = runner_mod._normalize_reflect_edits(reply, f"# Current skill\n{skill}")
        self.assertEqual(
            json.loads(out)["edits"], [{"op": "replace", "anchor": "旧规则", "content": "新规则"}]
        )

    def test_env_model_does_not_share_http_client(self) -> None:
        settings = {
            "provider": "OpenAI",
            "api_key": "k",
            "api_base": "http://env",
            "model_name": "env-model",
        }
        with patch.object(runner_mod, "_env_model_settings", return_value=(settings, [])):
            model, name = runner_mod._build_env_model()
        self.assertEqual(name, "env-model")
        self.assertFalse(model.model_client_config.use_shared_llm_http_client)

    def test_no_spec_uses_env_model(self) -> None:
        fake_model = MagicMock()
        with patch.object(
            runner_mod, "_build_env_model", return_value=(fake_model, "env-model")
        ) as env_model:
            client = runner_mod._build_chat_client(None)
        env_model.assert_called_once()
        self.assertIs(client.llm, fake_model)
        self.assertEqual(client.model, "env-model")


class TestAdapterSkillSleepModelSpec(unittest.TestCase):
    def test_skill_sleep_published_push_payload(self) -> None:
        from jiuwenswarm.server.runtime.agent_adapter import interface_deep
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            JiuWenSwarmDeepAdapter,
        )

        transport = MagicMock()
        transport.send_push = AsyncMock()
        with patch(
            "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
            return_value=transport,
        ), patch.object(
            interface_deep,
            "build_server_push_message",
            side_effect=lambda **kw: kw,
        ):
            asyncio.run(
                JiuWenSwarmDeepAdapter._push_skill_sleep_published(
                    SimpleNamespace(),
                    skill_name="tianqi",
                    version="1.1.0",
                    session_id="sess-1",
                    request_id="skill-sleep-6",
                )
            )
        message = transport.send_push.await_args.args[0]
        self.assertEqual(message["session_id"], "sess-1")
        self.assertEqual(message["request_id"], "skill-sleep-6")
        self.assertIsNone(message["fallback_channel_id"])
        self.assertEqual(
            message["payload"],
            {
                "event_type": "chat.evolution_published",
                "skill_name": "tianqi",
                "version": "1.1.0",
                "source": "skill_sleep",
                "request_id": "skill-sleep-6",
            },
        )

    def test_skill_sleep_suggest_push_payload(self) -> None:
        from jiuwenswarm.server.runtime.agent_adapter import interface_deep
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            JiuWenSwarmDeepAdapter,
        )

        transport = MagicMock()
        transport.send_push = AsyncMock()
        with patch(
            "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
            return_value=transport,
        ), patch.object(
            interface_deep,
            "build_server_push_message",
            side_effect=lambda **kw: kw,
        ):
            asyncio.run(
                JiuWenSwarmDeepAdapter._push_skill_sleep_suggest(
                    SimpleNamespace(),
                    skill_name="tianqi",
                    session_id="sess-1",
                    request_id="skill-sleep-7",
                )
            )
        message = transport.send_push.await_args.args[0]
        self.assertEqual(message["session_id"], "sess-1")
        self.assertEqual(message["request_id"], "skill-sleep-7")
        self.assertIsNone(message["fallback_channel_id"])
        self.assertEqual(message["payload"]["event_type"], "chat.evolution_status")
        self.assertEqual(message["payload"]["status"], "end")
        self.assertEqual(message["payload"]["stage"], "completed")
        self.assertEqual(message["payload"]["request_id"], "skill-sleep-7")
        self.assertIn("tianqi", message["payload"]["message"])

    def test_resolves_override_from_skill_sleep_config(self) -> None:
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            JiuWenSwarmDeepAdapter,
        )

        resolved = SimpleNamespace(
            model_client_config={"api_base": "http://lite"},
            model_config=SimpleNamespace(model_name="lite-model"),
        )
        stub = SimpleNamespace(
            _model=object(),
            _default_model_name="main-model",
            _config_cache={
                "react": {
                    "evolution": {
                        "skill_sleep": {"model_name": "", "model_tier": "lite"}
                    }
                }
            },
            _resolve_model=MagicMock(return_value=(resolved, None)),
        )
        spec = JiuWenSwarmDeepAdapter._skill_sleep_model_spec(stub)
        stub._resolve_model.assert_called_once_with(model_name="", model_tier="lite")
        self.assertEqual(spec.model_name, "lite-model")
        self.assertEqual(spec.client_config, {"api_base": "http://lite"})

    def test_returns_none_without_main_model(self) -> None:
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            JiuWenSwarmDeepAdapter,
        )

        stub = SimpleNamespace(_model=None, _config_cache={})
        self.assertIsNone(JiuWenSwarmDeepAdapter._skill_sleep_model_spec(stub))

    def test_forget_session_reaches_own_and_session_adapter_rails(self) -> None:
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            JiuWenSwarmDeepAdapter,
        )

        own, child_rail = MagicMock(), MagicMock()
        child_rail.forget_session.side_effect = RuntimeError("boom")
        stub = SimpleNamespace(_skill_sleep_rail=own)
        child = SimpleNamespace(_skill_sleep_rail=child_rail)
        JiuWenSwarmDeepAdapter._forget_skill_sleep_session(stub, "sess-1", child)
        own.forget_session.assert_called_once_with("sess-1")
        child_rail.forget_session.assert_called_once_with("sess-1")

        same = SimpleNamespace(_skill_sleep_rail=own)
        JiuWenSwarmDeepAdapter._forget_skill_sleep_session(stub, "sess-2", same)
        own.forget_session.assert_called_with("sess-2")
        self.assertEqual(own.forget_session.call_count, 2)


if __name__ == "__main__":
    unittest.main()
