# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""EvolutionService - Unified facade for the skill evolution system."""
from __future__ import annotations

import logging
import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

from jiuwenclaw.evolution.evolver import SkillEvolver
from jiuwenclaw.evolution.schema import (
    EvolutionEntry,
    EvolutionSignal,
    EvolutionType,
    ExperienceContext,
)
from jiuwenclaw.evolution.signal_detector import SignalDetector
from jiuwenclaw.evolution.store import EvolutionStore

logger = logging.getLogger(__name__)

_APPROVAL_TIMEOUT = 300  # seconds
_MAX_PROCESSED_SIGNAL_KEYS = 500  # safety cap to prevent unbounded growth across sessions


class EvolutionService:
    """Unified facade for the skill online evolution system.

    Owns SignalDetector, SkillEvolver, and EvolutionStore.
    Handles the complete lifecycle: detect -> deduplicate -> route -> generate
    -> approve -> persist.
    """

    def __init__(
        self,
        llm: Any,
        model: str,
        skills_base_dir: str,
        auto_scan: bool = False,
    ) -> None:
        self._store = EvolutionStore(skills_base_dir)
        self._evolver = SkillEvolver(llm, model)
        self._auto_scan = auto_scan
        self._pending_approvals: Dict[str, asyncio.Future] = {}
        self._processed_signal_keys: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def auto_scan(self) -> bool:
        return self._auto_scan

    @auto_scan.setter
    def auto_scan(self, value: bool) -> None:
        self._auto_scan = value

    @property
    def skills_base_dir(self) -> str:
        return str(self._store.base_dir)

    @property
    def store(self) -> EvolutionStore:
        return self._store

    # ------------------------------------------------------------------
    # Hot-reload
    # ------------------------------------------------------------------

    def update_llm(self, llm: Any, model: str) -> None:
        self._evolver.update_llm(llm, model)

    def clear_processed_signals(self) -> None:
        """Clear the processed-signal fingerprint cache.

        Call this at conversation boundaries so signals from the previous
        conversation do not suppress detection in the next one.
        """
        self._processed_signal_keys.clear()
        logger.info("[EvolutionService] processed signal keys cleared")

    # ------------------------------------------------------------------
    # Manual trigger: /evolve command
    # ------------------------------------------------------------------

    async def handle_evolve_command(
        self,
        query: str,
        session: Any,
        messages: List[Any],
    ) -> Dict[str, Any]:
        """/evolve [list | <skill_name>] command handler.

        Args:
            query: Raw user input starting with /evolve.
            session: Session for streaming / approval.
            messages: Raw history messages (BaseMessage or dict); parsed internally.
        """
        skill_names = self._store.list_skill_names()

        parts = query.split(maxsplit=1)
        skill_arg = parts[1].strip() if len(parts) > 1 else ""

        if not skill_arg or skill_arg == "list":
            if not skill_names:
                return {
                    "output": "当前 skills_base_dir 下未找到任何 Skill 目录。",
                    "result_type": "answer",
                }
            summary = self._store.list_pending_summary(skill_names)
            return {
                "output": f"**Skills 演进记录：**\n\n{summary}",
                "result_type": "answer",
            }

        skill_name = skill_arg
        if skill_name not in skill_names:
            available = "、".join(skill_names) or "（无可用 Skill）"
            return {
                "output": (
                    f"在 skills_base_dir 下未找到 Skill '{skill_name}'。\n"
                    f"当前可用 Skill：{available}\n"
                    f"可使用 /evolve list 查看所有记录。"
                ),
                "result_type": "error",
            }

        parsed = self._parse_messages(messages)
        signals = self._detect_signals(parsed, skill_names)
        if not signals:
            return {
                "output": "当前对话未发现明确的演进信号（无工具执行失败、无用户纠正）。\n",
                "result_type": "answer",
            }
        attributed = [s for s in signals if s.skill_name == skill_name]
        entries = await self._generate_experience_for_skill(skill_name, attributed, parsed)
        if not entries:
            return {
                "output": "当前对话未发现明确的演进信号（无工具执行失败、无用户纠正）。\n",
                "result_type": "answer",
            }

        if session is not None:
            tagged = [(skill_name, e) for e in entries]
            kept = await self._request_batch_approval(session, tagged)
            kept_entries = [entry for _, entry in kept]
            for sn, entry in kept:
                self._store.append_entry(sn, entry)
            if kept_entries:
                summaries = "\n".join(
                    f"  {i+1}. **[{e.change.section}]** {e.change.content[:200]}"
                    for i, e in enumerate(kept_entries)
                )
                return {
                    "output": (
                        f"已记录 {len(kept_entries)} 条演进经验到 Skill '{skill_name}'：\n"
                        f"{summaries}\n\n"
                        f"（evolutions.json 已更新，自动生效；"
                        f"可使用 `/solidify {skill_name}` 将经验固化到 SKILL.md 本体）"
                    ),
                    "result_type": "answer",
                }
            return {
                "output": f"已丢弃 Skill '{skill_name}' 的全部演进内容，evolutions.json 未变更。",
                "result_type": "answer",
            }

        for entry in entries:
            self._store.append_entry(skill_name, entry)
        summaries = "\n".join(
            f"  {i+1}. **[{e.change.section}]** {e.change.content[:200]}"
            for i, e in enumerate(entries)
        )
        return {
            "output": (
                f"已记录 {len(entries)} 条演进经验到 Skill '{skill_name}'：\n"
                f"{summaries}"
            ),
            "result_type": "answer",
        }

    # ------------------------------------------------------------------
    # Auto trigger: after conversation round
    # ------------------------------------------------------------------

    async def run_auto_evolution(
        self,
        session: Any,
        history_messages: List[Any],
    ) -> None:
        """Auto-scan after a conversation round, generate + approve + persist."""
        skill_names = self._store.list_skill_names()
        if not skill_names:
            return

        parsed = self._parse_messages(history_messages)
        signals = self._detect_signals(parsed, skill_names)
        if not signals:
            return

        await self._route_and_process(signals, parsed, session)

    # ------------------------------------------------------------------
    # Solidify command
    # ------------------------------------------------------------------

    def handle_solidify_command(self, query: str) -> Dict[str, Any]:
        """/solidify <skill_name> handler."""
        parts = query.split(maxsplit=1)
        skill_name = parts[1].strip() if len(parts) > 1 else ""
        if not skill_name:
            return {
                "output": "请指定 Skill 名称：`/solidify <skill_name>`",
                "result_type": "error",
            }
        count = self._store.solidify(skill_name)
        if count == 0:
            msg = f"Skill '{skill_name}' 没有待固化的演进经验。"
        else:
            msg = f"已将 {count} 条演进经验固化到 Skill '{skill_name}' 的 SKILL.md。"
        return {"output": msg, "result_type": "answer"}

    # ------------------------------------------------------------------
    # Approval flow (migrated from react_agent.py)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_approval_questions(
        entries: List[tuple],
    ) -> list:
        """Build the questions payload for a batch approval request."""
        questions = []
        for skill_name, entry in entries:
            content_preview = entry.change.content[:1000]
            questions.append({
                "question": (
                    f"**Skill '{skill_name}' 演进生成了新内容：**\n\n"
                    f"{content_preview}"
                ),
                "header": "演进审批",
                "options": [
                    {"label": "接收", "description": "保留此演进经验"},
                    {"label": "拒绝", "description": "丢弃此演进经验"},
                ],
                "multi_select": False,
            })
        return questions

    @staticmethod
    def _parse_approval_answers(
        answers: list,
        entries: List[tuple],
    ) -> List[tuple]:
        """Return the (skill_name, entry) pairs that user chose to keep."""
        kept: List[tuple] = []
        for i, (skill_name, entry) in enumerate(entries):
            keep = (
                i < len(answers)
                and isinstance(answers[i], dict)
                and "接收" in answers[i].get("selected_options", [])
            )
            if keep:
                kept.append((skill_name, entry))
        return kept

    async def _request_batch_approval(
        self,
        session: Any,
        entries: List[tuple],
    ) -> List[tuple]:
        """Send a single batch approval request and block until answered.

        Used by /evolve command where we need a synchronous result.
        Returns the (skill_name, entry) pairs that user chose to keep.
        """
        from openjiuwen.core.session.stream import OutputSchema

        request_id = f"evolve_approve_{uuid.uuid4().hex[:8]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_approvals[request_id] = future

        questions = self._build_approval_questions(entries)
        try:
            await session.write_stream(
                OutputSchema(
                    type="chat.ask_user_question",
                    index=0,
                    payload={"request_id": request_id, "questions": questions},
                )
            )
        except Exception:
            logger.debug("[EvolutionService] batch approval send failed", exc_info=True)
            self._pending_approvals.pop(request_id, None)
            return list(entries)

        try:
            answers = await asyncio.wait_for(future, timeout=_APPROVAL_TIMEOUT)
            return self._parse_approval_answers(answers, entries)
        except asyncio.TimeoutError:
            logger.info("[EvolutionService] batch approval timeout, auto-keeping all")
            return list(entries)
        finally:
            self._pending_approvals.pop(request_id, None)

    async def _request_batch_approval_async(
        self,
        session: Any,
        entries: List[tuple],
    ) -> None:
        """Send a single batch approval request then return immediately.

        A background task handles the wait and persistence so the stream
        session is not blocked by the approval timeout.
        """
        from openjiuwen.core.session.stream import OutputSchema

        request_id = f"evolve_approve_{uuid.uuid4().hex[:8]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_approvals[request_id] = future

        questions = self._build_approval_questions(entries)
        try:
            await session.write_stream(
                OutputSchema(
                    type="chat.ask_user_question",
                    index=0,
                    payload={"request_id": request_id, "questions": questions},
                )
            )
        except Exception:
            logger.debug("[EvolutionService] batch approval send failed", exc_info=True)
            self._pending_approvals.pop(request_id, None)
            for skill_name, entry in entries:
                self._store.append_entry(skill_name, entry)
            logger.info(
                "[EvolutionService] send failed, auto-keeping %d entries", len(entries),
            )
            return

        asyncio.create_task(
            self._wait_and_persist_batch(future, request_id, entries)
        )

    async def _wait_and_persist_batch(
        self,
        future: asyncio.Future,
        request_id: str,
        entries: List[tuple],
    ) -> None:
        """Background task: wait for batch approval then persist kept entries."""
        try:
            answers = await asyncio.wait_for(future, timeout=_APPROVAL_TIMEOUT)
            kept = self._parse_approval_answers(answers, entries)
            for skill_name, entry in kept:
                self._store.append_entry(skill_name, entry)
                logger.info("[EvolutionService] kept: skill=%s id=%s", skill_name, entry.id)
            discarded = len(entries) - len(kept)
            if discarded:
                logger.info("[EvolutionService] discarded %d entries", discarded)
        except asyncio.TimeoutError:
            logger.info(
                "[EvolutionService] batch approval timeout, auto-discarding %d entries",
                len(entries),
            )
        except asyncio.CancelledError:
            for skill_name, entry in entries:
                self._store.append_entry(skill_name, entry)
            logger.info(
                "[EvolutionService] batch approval cancelled, auto-keeping %d entries",
                len(entries),
            )
        except Exception as exc:
            logger.warning("[EvolutionService] batch approval wait error: %s", exc)
        finally:
            self._pending_approvals.pop(request_id, None)

    def resolve_approval(self, request_id: str, answers: list) -> bool:
        """Resolve a pending approval future with user's answer.

        Called by interface.py on chat.user_answer.
        Passes the raw answers list to the waiting future so that both
        _request_batch_approval and _wait_and_persist_batch can interpret it.
        Returns True if resolved, False if not found.
        """
        future = self._pending_approvals.get(request_id)
        if future is None or future.done():
            return False

        future.set_result(answers)
        logger.info(
            "[EvolutionService] approval resolved: request_id=%s answers=%d",
            request_id,
            len(answers),
        )
        return True

    # ------------------------------------------------------------------
    # Core internal: detect -> deduplicate -> route
    # ------------------------------------------------------------------

    def _detect_signals(
        self,
        parsed_messages: List[dict],
        skill_names: List[str],
    ) -> List[EvolutionSignal]:
        """Call SignalDetector and filter already-processed signals by fingerprint.

        Args:
            parsed_messages: Already-normalized message dicts (call _parse_messages upstream).
            skill_names: Known skill directory names.
        """
        skill_dir_map = {
            name: str(self._store.base_dir / name / "SKILL.md")
            for name in skill_names
            if self._store.skill_exists(name)
        }
        detector = SignalDetector(skill_dir_map=skill_dir_map)
        signals = detector.detect(parsed_messages)

        new_signals = [
            sig for sig in signals
            if (sig.type, sig.excerpt[:100]) not in self._processed_signal_keys
        ]
        for sig in new_signals:
            self._processed_signal_keys.add((sig.type, sig.excerpt[:100]))

        if len(self._processed_signal_keys) > _MAX_PROCESSED_SIGNAL_KEYS:
            logger.info(
                "[EvolutionService] _processed_signal_keys exceeded cap (%d), clearing",
                _MAX_PROCESSED_SIGNAL_KEYS,
            )
            self._processed_signal_keys.clear()

        if new_signals:
            logger.info(
                "[EvolutionService] detected %d new signal(s) (filtered %d already-processed): %s",
                len(new_signals),
                len(signals) - len(new_signals),
                json.dumps([s.to_dict() for s in new_signals], ensure_ascii=False),
            )
        return new_signals

    async def _route_and_process(
        self,
        signals: List[EvolutionSignal],
        parsed_messages: List[dict],
        session: Any,
    ) -> None:
        """Route signals to the appropriate handler, grouped by skill.

        Signals attributed to the same skill are batched into a single LLM call
        to avoid redundant generation and duplicate experiences.
        """
        skill_groups: Dict[str, List[EvolutionSignal]] = {}
        for sig in signals:
            if sig.evolution_type == EvolutionType.SKILL_EXPERIENCE and sig.skill_name:
                skill_groups.setdefault(sig.skill_name, []).append(sig)
            # Future: elif sig.evolution_type == EvolutionType.NEW_SKILL:
            #             await self._evolve_new_skill(...)

        all_entries: List[tuple] = []
        for skill_name, skill_signals in skill_groups.items():
            entries = await self._generate_experience_for_skill(
                skill_name, skill_signals, parsed_messages
            )
            for entry in entries:
                all_entries.append((skill_name, entry))

        if all_entries:
            try:
                await self._request_batch_approval_async(session, all_entries)
            except Exception as exc:
                logger.warning(
                    "[EvolutionService] batch approval error: %s", exc,
                )

    async def _generate_experience_for_skill(
        self,
        skill_name: str,
        signals: List[EvolutionSignal],
        messages: List[dict],
    ) -> List[EvolutionEntry]:
        """Generate evolution entries for a single skill."""
        ctx = ExperienceContext(
            skill_name=skill_name,
            signals=signals,
            skill_content=self._store.read_skill_content(skill_name),
            messages=messages,
            existing_desc_entries=self._store.load_desc_pending_entries(skill_name),
            existing_body_entries=self._store.load_body_pending_entries(skill_name),
        )
        try:
            return await self._evolver.generate_skill_experience(ctx)
        except Exception as exc:
            logger.warning(
                "[EvolutionService] generate failed (skill=%s): %s", skill_name, exc
            )
            return []

    @staticmethod
    def extract_user_content(raw: str) -> str:
        """Extract decoded user content from build_user_prompt JSON wrapper.

        The wrapper format is:
            你收到一条消息：\n{"source": ..., "content": "...", ...}
        Returns the inner *content* value (with unicode escapes decoded),
        or falls back to *raw* if parsing fails.
        """
        prefix = "你收到一条消息：\n"
        if not raw.startswith(prefix):
            return raw
        json_part = raw[len(prefix):]
        try:
            payload = json.loads(json_part)
            if isinstance(payload, dict) and "content" in payload:
                return payload["content"]
        except (json.JSONDecodeError, TypeError):
            pass
        return raw

    @staticmethod
    def _parse_messages(messages: List[Any]) -> List[dict]:
        """Normalize BaseMessage or dict messages to plain dicts."""
        result: List[dict] = []
        for msg in messages:
            if isinstance(msg, dict):
                result.append(msg)
            elif hasattr(msg, "role"):
                content = str(getattr(msg, "content", "") or "")
                role = getattr(msg, "role", "")
                if role == "user":
                    content = EvolutionService.extract_user_content(content)
                d: dict = {"role": role, "content": content}
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    d["tool_calls"] = [
                        {
                            "id": getattr(tc, "id", ""),
                            "name": getattr(tc, "name", ""),
                            "arguments": getattr(tc, "arguments", ""),
                        }
                        for tc in tool_calls
                    ]
                name = getattr(msg, "name", None)
                if name:
                    d["name"] = name
                result.append(d)
        return result
