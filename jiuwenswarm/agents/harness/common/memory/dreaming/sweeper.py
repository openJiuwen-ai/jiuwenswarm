# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Dreaming Sweeper: Scan + Compression + LLM Extraction + Promotion (Unified Pipeline)"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.session.session_history import (
    _read_history,
    _read_history_jsonl,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (parameters not configurable externally)
# ---------------------------------------------------------------------------
_MIN_SESSION_ROUNDS = 4
_MAX_SESSIONS_PER_SWEEP = 10
_MAX_SESSION_AGE_DAYS = 30
_MAX_COMPRESS_TOKENS = 30000
_MAX_ENTRIES_AGENT = 50
_MAX_PROMOTIONS_PER_SESSION = 5
_MIN_NEW_SESSIONS = 3
_MIN_SESSION_AGE_BYPASS_DAYS = 7


# ---------------------------------------------------------------------------
# Configuration (only keep fields that need external control)
# ---------------------------------------------------------------------------
@dataclass
class DreamingConfig:
    enabled: bool = False
    interval_seconds: float = 14400.0

    @classmethod
    def load(cls, mode: str = "code") -> "DreamingConfig":
        """Load configuration from the config.yaml memory.dreaming.{mode} or return enabled=False."""
        try:
            from jiuwenswarm.common.config import get_config
            raw = get_config().get("memory", {}).get("dreaming", {}).get(mode, {})
            if not isinstance(raw, dict):
                raw = {}
            env_key = f"DREAMING_{mode.upper()}_ENABLED"
            env_val = os.getenv(env_key)
            if env_val is not None:
                enabled = env_val.lower() in ("true", "1", "yes")
            else:
                enabled = bool(raw.get("enabled", False))
            interval = float(os.getenv(
                "DREAMING_INTERVAL",
                str(raw.get("interval_seconds", 14400.0)),
            ))
            return cls(enabled=enabled, interval_seconds=interval)
        except Exception as exc:
            logger.warning("[dreaming] %s configuration load failed, using default values: %s", mode, exc)
            return cls()


# ---------------------------------------------------------------------------
# UI text i18n
# ---------------------------------------------------------------------------
_UI_TEXT = {
    "zh": {
        "empty_summary": "(空)",
        "dreaming_header": "# Dreaming 记忆\n",
        "truncate_rounds": "[... 前 {keep_from} 轮对话已省略 ...]\n\n",
        "truncate_hard": "[... 内容过长，已截断 ...]\n\n",
    },
    "en": {
        "empty_summary": "(None)",
        "dreaming_header": "# Dreaming Memories\n",
        "truncate_rounds": "[... First {keep_from} rounds omitted ...]\n\n",
        "truncate_hard": "[... Content too long, truncated ...]\n\n",
    },
}


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------
class Sweeper:
    """Unified dreaming pipeline: Scan + Compression + LLM Extraction + Promotion."""

    def __init__(self, sessions_dir: str, output_dir: str, mode: str = "code", language: str = "zh") -> None:
        self._sessions_dir = sessions_dir
        self._output_dir = output_dir
        self._mode = mode
        self._language = language
        self._dreams_dir = Path(output_dir) / ".dreams"
        self.scanned_sessions: dict[str, dict] = {}

        self._prompt_map = {
            ("code", "zh"): _PROMPT_CODE,
            ("code", "en"): _PROMPT_CODE_EN,
            ("agent", "zh"): _PROMPT_AGENT,
            ("agent", "en"): _PROMPT_AGENT_EN,
        }
        self._sys_msg_map = {
            ("code", "zh"): "你是技术经验提取助手。严格输出 JSON 数组。",
            ("code", "en"): "You are a technical knowledge extractor. Output JSON array strictly.",
            ("agent", "zh"): "你是记忆整理助手。严格输出 JSON 数组。",
            ("agent", "en"): "You are a memory organizer. Output JSON array strictly.",
        }

    def init(self) -> None:
        """Create directories and load the checkpoint."""
        self._dreams_dir.mkdir(parents=True, exist_ok=True)
        Path(self._output_dir).mkdir(parents=True, exist_ok=True)
        cp = self._load_checkpoint()
        raw = cp.get("scanned_sessions", [])
        if isinstance(raw, list):
            self.scanned_sessions = {sid: {} for sid in raw}
        elif isinstance(raw, dict):
            self.scanned_sessions = raw
        else:
            self.scanned_sessions = {}

    async def run_sweep(self) -> None:
        """Complete pipeline. Called by the Orchestrator."""
        sweep_start = time.monotonic()

        # ── Scan + Pre-filter + Compression ──
        try:
            sessions = await asyncio.get_running_loop().run_in_executor(
                None, self.scan_new_sessions,
            )
        except Exception:
            logger.exception("[Sweeper] Scan stage failed")
            return

        if not sessions:
            logger.debug("[Sweeper] No eligible session found")
            return

        # ── LLM Extraction ──
        existing_summary = self.load_existing_summary()
        all_knowledge: list[dict] = []
        succeeded_ids: list[str] = []
        failed_ids: list[str] = []

        for s in sessions:
            try:
                items = await self._extract_via_llm(
                    s["compressed_text"], existing_summary,
                )
                for item in items:
                    item["source_session_id"] = s["session_id"]
                all_knowledge.extend(items)
                succeeded_ids.append(s["session_id"])
            except Exception:
                logger.exception("[Sweeper] LLM Extraction for session %s failed", s["session_id"])
                failed_ids.append(s["session_id"])

        # ── Promotion ──
        if succeeded_ids:
            sessions_root = Path(self._sessions_dir)
            for sid in succeeded_ids:
                history_mtime = self._get_session_history_mtime(sid) or 0.0
                events = self._parse_history(sessions_root / sid)
                rounds = self._detect_rounds(events)
                self.scanned_sessions[sid] = {
                    "history_mtime": history_mtime,
                    "round_count": len(rounds),
                }
            self.save_checkpoint()

        promoted = 0
        for k in all_knowledge:
            try:
                result = self._promote(
                    k.get("title", ""), k.get("content", ""),
                    k.get("source_session_id", ""),
                )
                if result is not None:
                    promoted += 1
            except Exception:
                logger.exception("[Sweeper] Promotion failed: %s", k.get("title", ""))

        duration = time.monotonic() - sweep_start
        logger.info(
            "[Sweeper] sweep completed: duration=%.1fs sessions=%d "
            "llm_succeeded=%d llm_failed=%d extracted=%d promoted=%d",
            duration, len(sessions), len(succeeded_ids),
            len(failed_ids), len(all_knowledge), promoted,
        )

    # =====================================================================
    # Scan + Compression
    # =====================================================================

    def scan_new_sessions(self) -> list[dict]:
        """Incremental scan session directories and return a list of new/updated sessions with compressed text."""
        sessions_root = Path(self._sessions_dir)
        if not sessions_root.exists():
            return []

        all_ids = {d.name for d in sessions_root.iterdir() if d.is_dir() and not d.name.startswith("heartbeat")}
        scanned_ids = set(self.scanned_sessions.keys())
        brand_new = all_ids - scanned_ids

        new_ids = brand_new.copy()

        for sid in all_ids & scanned_ids:
            current_mtime = self._get_session_history_mtime(sid)
            if current_mtime is None:
                continue
            snapshot = self.scanned_sessions.get(sid, {})
            if current_mtime <= snapshot.get("history_mtime", 0):
                continue
            new_ids.add(sid)

        if len(new_ids) < _MIN_NEW_SESSIONS:
            age_bypass = _MIN_SESSION_AGE_BYPASS_DAYS * 86400
            has_old = any(
                (time.time() - (sessions_root / sid).stat().st_mtime) > age_bypass
                for sid in new_ids
                if (sessions_root / sid).exists()
            )
            if not has_old:
                logger.info(
                    "[Sweeper] %d new sessions < %d and no expired sessions, skipping",
                    len(new_ids), _MIN_NEW_SESSIONS,
                )
                return []

        results: list[dict] = []
        now = time.time()
        max_age = _MAX_SESSION_AGE_DAYS * 86400

        for session_id in sorted(new_ids):
            session_dir = sessions_root / session_id

            try:
                mtime = session_dir.stat().st_mtime
            except OSError:
                continue
            if now - mtime > max_age:
                self.scanned_sessions[session_id] = {}
                continue

            is_incremental = session_id in self.scanned_sessions and self.scanned_sessions.get(session_id)

            if not self._match_session_mode(session_id):
                continue

            try:
                events = self._parse_history(session_dir)
                if not events:
                    continue
                rounds = self._detect_rounds(events)
                if is_incremental:
                    last_count = self.scanned_sessions[session_id].get("round_count", 0)
                    new_rounds = len(rounds) - last_count
                    if new_rounds <= 0:
                        continue
                    if new_rounds < _MIN_SESSION_ROUNDS:
                        continue
                    incremental_events = events[rounds[last_count][0]:]
                    compressed = self._compress(incremental_events)
                else:
                    if len(rounds) < _MIN_SESSION_ROUNDS:
                        continue
                    compressed = self._compress(events)
                if not compressed.strip():
                    continue
                results.append({
                    "session_id": session_id,
                    "compressed_text": compressed,
                })
                if len(results) >= _MAX_SESSIONS_PER_SWEEP:
                    break
            except Exception as exc:
                logger.warning("[Sweeper] Scan session %s failed: %s", session_id, exc)

        return results

    def _match_session_mode(self, session_id: str) -> bool:
        meta_path = Path(self._sessions_dir) / session_id / "metadata.json"
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        session_mode = str(data.get("mode", "")).strip()
        if not session_mode:
            return False
        return session_mode.startswith(self._mode)

    def _get_session_history_mtime(self, session_id: str) -> float | None:
        session_dir = Path(self._sessions_dir) / session_id
        history_jsonl = session_dir / "history.jsonl"
        history_json = session_dir / "history.json"
        history_path = history_jsonl if history_jsonl.exists() else history_json
        if not history_path.exists():
            return None
        try:
            return history_path.stat().st_mtime
        except OSError:
            return None

    @staticmethod
    def _parse_history(session_dir: Path) -> list[dict]:
        history_jsonl = session_dir / "history.jsonl"
        history_json = session_dir / "history.json"
        try:
            if history_jsonl.exists():
                data = _read_history_jsonl(history_jsonl)
            else:
                data = _read_history(history_json)
            if not isinstance(data, list):
                return []
        except Exception:
            return []

        def _should_keep(entry: dict) -> bool:
            if entry.get("role") == "user":
                return True
            if entry.get("event_type", "") == "chat.final":
                return True
            if entry.get("role") == "assistant" and "event_type" not in entry:
                return True
            return False

        return [e for e in data if _should_keep(e)]

    @staticmethod
    def _detect_rounds(events: list[dict]) -> list[tuple[int, int]]:
        current_user = -1
        pairs: list[tuple[int, int]] = []
        for i, e in enumerate(events):
            role = e.get("role", "")
            if role == "user":
                current_user = i
            elif role == "assistant" and current_user != -1:
                pairs.append((current_user, i))
                current_user = -1
        return pairs

    def _compress(self, events: list[dict], _depth: int = 0) -> str:
        max_recurse = 5
        _is_code = (self._mode or "").startswith("code")

        parts: list[str] = []
        for e in events:
            role = e.get("role", "")
            content = str(e.get("content", ""))
            if role == "user":
                parts.append(f"[User]: {content[:2000]}")
            elif not _is_code and role == "assistant" and content.strip():
                parts.append(f"[Assistant]: {content[:3000]}")

        result = "\n\n".join(parts)

        est_tokens = len(result) // 2
        if est_tokens > _MAX_COMPRESS_TOKENS:
            if _depth < max_recurse:
                rounds = Sweeper._detect_rounds(events)
                if len(rounds) > 2:
                    keep_from = len(rounds) // 3
                    trimmed = events[rounds[keep_from][0]:]
                    prefix = _UI_TEXT[self._language]["truncate_rounds"].format(keep_from=keep_from)
                    return prefix + self._compress(trimmed, _depth + 1)

            max_chars = _MAX_COMPRESS_TOKENS * 2
            result = _UI_TEXT[self._language]["truncate_hard"] + result[-max_chars:]

        return result

    # =====================================================================
    # LLM Extraction
    # =====================================================================

    async def _extract_via_llm(
        self, compressed_text: str, existing_summary: str,
    ) -> list[dict]:
        from jiuwenswarm.common.config import get_default_models
        from openjiuwen.core.foundation.llm import (
            Model, ModelClientConfig, ModelRequestConfig,
            UserMessage, SystemMessage,
        )

        entries = get_default_models()
        if not entries:
            logger.warning("[Sweeper] No default models configured")
            return []

        entry = entries[0]
        mcc = entry.get("model_client_config", {})
        model_name = mcc.get("model_name", "")
        mcc_fields = {k: v for k, v in mcc.items() if k != "model_name"}
        model = Model(
            model_client_config=ModelClientConfig(**mcc_fields),
            model_config=ModelRequestConfig(model=model_name, temperature=0.3),
        )

        key = (self._mode, self._language)
        prompt_template = self._prompt_map.get(key, _PROMPT_CODE)
        prompt = prompt_template.format(
            existing_knowledge=existing_summary,
            compressed_session=compressed_text,
            max_items=_MAX_PROMOTIONS_PER_SESSION,
        )
        sys_msg = self._sys_msg_map.get(key, self._sys_msg_map[("code", "zh")])

        try:
            response = await model.invoke([
                SystemMessage(content=sys_msg),
                UserMessage(content=prompt),
            ])
            content = self._extract_content_str(response.content)
            json_match = re.search(r'```json\s*\n?(.*?)\n?```', content, re.DOTALL)
            if not json_match:
                json_match = re.search(r'```\s*\n?(.*?)\n?```', content, re.DOTALL)
            json_str = json_match.group(1) if json_match else content
            parsed = json.loads(json_str.strip())
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            logger.warning("[Sweeper] LLM output JSON parse failed")
            return []
        except Exception as exc:
            logger.warning("[Sweeper] LLM call failed: %s", exc)
            return []

    @staticmethod
    def _extract_content_str(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get("text", ""))
            return "\n".join(parts)
        return str(content) if content else ""

    def load_existing_summary(self) -> str:
        empty_label = _UI_TEXT.get(self._language, _UI_TEXT["zh"])["empty_summary"]
        output = Path(self._output_dir)
        if not output.exists():
            return empty_label

        if self._mode == "agent":
            dreaming_path = output / "DREAMING.md"
            if not dreaming_path.exists():
                return empty_label
            try:
                text = dreaming_path.read_text(encoding="utf-8", errors="replace")
                titles = re.findall(r'^## (.+)$', text, re.MULTILINE)
                return "\n".join(f"- {t}" for t in titles) if titles else empty_label
            except Exception:
                return empty_label

        lines: list[str] = []
        for f in sorted(output.glob("consolidated_*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                m = re.search(r'^name:\s*(.+)$', text, re.MULTILINE)
                lines.append(f"- {m.group(1).strip()}" if m else f"- {f.stem}")
            except Exception:
                logger.warning("[dreaming] failed to read consolidated file %s: %s", f, exc_info=True)
                continue
        return "\n".join(lines) if lines else empty_label

    # =====================================================================
    # Promotion
    # =====================================================================

    def _promote(self, title: str, content: str, session_id: str) -> str | None:
        if not title or not content:
            return None
        if self._mode == "agent":
            return self.promote_agent(title, content, session_id)
        return self.promote_code(title, content, session_id)

    def promote_agent(self, title: str, content: str, session_id: str) -> str | None:
        dreaming_path = Path(self._output_dir) / "DREAMING.md"
        header = _UI_TEXT.get(self._language, _UI_TEXT["zh"])["dreaming_header"]

        if dreaming_path.exists():
            try:
                existing = dreaming_path.read_text(encoding="utf-8")
            except OSError:
                existing = header
        else:
            existing = header

        entries = self._parse_dreaming_entries(existing)

        if any(e["title"] == title for e in entries):
            return None

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        entries.append({
            "title": title,
            "source": f"_source: {session_id} | {today}",
            "content": content,
        })

        if len(entries) > _MAX_ENTRIES_AGENT:
            entries = entries[len(entries) - _MAX_ENTRIES_AGENT:]

        parts = [header]
        for e in entries:
            parts.append(f"\n## {e['title']}")
            if e.get("source"):
                parts.append(e["source"])
            parts.append(f"\n{e['content']}\n")
        dreaming_path.write_text("\n".join(parts), encoding="utf-8")
        return str(dreaming_path)

    @staticmethod
    def _parse_dreaming_entries(text: str) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for part in re.split(r'^## ', text, flags=re.MULTILINE)[1:]:
            lines = part.strip().splitlines()
            if not lines:
                continue
            title = lines[0].strip()
            source = ""
            content_lines: list[str] = []
            for line in lines[1:]:
                if line.startswith("_source:") and line.endswith("_"):
                    source = line
                else:
                    content_lines.append(line)
            entries.append({
                "title": title,
                "source": source,
                "content": "\n".join(content_lines).strip(),
            })
        return entries

    def promote_code(self, title: str, content: str, session_id: str) -> str | None:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        filename = f"consolidated_{content_hash}.md"
        output_path = Path(self._output_dir) / filename

        if output_path.exists():
            return None

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        body = (
            f"---\n"
            f"name: {title}\n"
            f"source_session: {session_id}\n"
            f"created_at: {today}\n"
            f"---\n\n"
            f"{content}\n"
        )
        output_path.write_text(body, encoding="utf-8")
        return str(output_path)

    # =====================================================================
    # checkpoint
    # =====================================================================

    def _load_checkpoint(self) -> dict:
        path = self._dreams_dir / "ingestion-checkpoint.json"
        try:
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else {}
                return data if isinstance(data, dict) else {}
        except Exception:
            logger.warning("[dreaming] failed to load checkpoint %s: %s", path, exc_info=True)
            pass
        return {}

    def save_checkpoint(self) -> None:
        path = self._dreams_dir / "ingestion-checkpoint.json"
        try:
            self._dreams_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "scanned_sessions": self.scanned_sessions,
                "last_scan_ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("[Sweeper] checkpoint save failed: %s", exc)


# =========================================================================
# LLM Prompt Templates
# =========================================================================

_PROMPT_CODE = """\
你是技术经验提取助手。下面是开发者与 AI 编程助手的对话（已压缩）。

目标：提取**只有亲历这次任务才能得到**的经验、踩坑、结论，帮用户在未来类似任务中少走弯路。
绝大多数对话沉淀不了知识，输出 [] 是常态，不要凑数。

## 值得提取（必须带"为什么/教训"，不是平铺事实）

- 踩坑根因：症状 X，常规思路 W 为何不通，真正根因 Y，解法 Z
- 被验证的非显而易见行为：API / 库 / 工具在边界情况下与文档或直觉不符的实际表现
- 设计决策的"为什么"：在哪些约束下选 A 不选 B，放弃 B 的代价
- 项目隐含约定：跨模块契约、不可违反的不变量、grep 难发现的命名 / 结构规则
- 环境 / 部署 / 工具链的坑：版本组合、平台差异、CI 行为
- 被否决的方案及原因

## 严禁提取

- AI 助手自身运行机制：内置工具行为、系统提示规则、记忆 / 钩子 / 技能等内部机制
- 通用编程常识：入门资料里都有的内容
- 对话流水账：做了什么的过程描述，而非沉淀出的结论
- 静态可查事实：grep / 看代码立即可得的信息
- 一次性任务状态：PR 编号、当前进度、临时调试目标
- 已在下方"已有知识库"中记录过的

## 自检三问（每条候选都过一遍）

1. 一周后做类似任务，没这条会怎样？看代码 / 查文档能想起的 → 丢弃
2. 这是关于用户的代码 / 项目 / 领域，还是关于 AI 助手的工具 / 机制？后者一律丢弃
3. 能否写成"因为 X 所以 Y / 不要 Z"或"X 出过问题，根因是 Y"？只能写成平铺事实 → 丢弃

## 已有知识库

{existing_knowledge}

## 对话记录

注：仅含用户提问和助手最终回复，无工具调用细节。无法判断时宁可不提取。

{compressed_session}

## 输出

JSON 数组。**绝大多数情况输出 []**。最多 {max_items} 条。

```json
[
  {{
    "title": "简洁标题（10-30 字，点明经验 / 教训）",
    "content": "背景 + 现象 + 根因 / 为什么 + 结论 / 做法，含具体示例，脱离对话上下文可独立阅读"
  }}
]
```

"""

_PROMPT_AGENT = """\
你是一个记忆整理助手。下面是一个用户与 AI 助手的对话记录（已压缩）。

请从中提取值得长期记住的信息。每条记忆应当脱离原始对话语境后仍然可读、可用。

## 提取标准

值得提取的：
- 用户明确表达的偏好（回复风格、语言习惯、关注重点等）
- 用户的个人背景信息（职业、技术栈、所在团队/公司、角色等）
- 用户提到的重要事实（项目名称、截止日期、关键人物等）
- 用户关心的领域知识或专业话题
- 用户纠正助手的地方（说明助手之前的认知有误）
- 反复出现的交互模式

不值得提取的：
- 一次性的事务请求
- 对话中的寒暄、确认、感谢
- 已在下方"已有记忆"中记录过的内容

## 已有记忆

{existing_knowledge}

## 对话记录

{compressed_session}

## 输出格式

输出 JSON 数组，每个元素是一条独立记忆。无值得提取的信息则输出 []。最多 {max_items} 条。

```json
[
  {{
    "title": "简洁的标题（10-30字）",
    "content": "完整的记忆描述，用陈述句描述事实，脱离对话上下文后可独立阅读"
  }}
]
```

要求：每条记忆独立成篇，宁缺毋滥。
"""

_PROMPT_CODE_EN = """\
You are a technical experience extraction assistant. Below is a compressed conversation between a developer and an AI coding assistant.

Goal: Extract experiences, pitfalls, and conclusions that can **only be gained by personally going through this task**, helping the user avoid detours in similar future tasks.
Most conversations yield no extractable knowledge — outputting [] is normal, don't force entries.

## Worth Extracting (must include "why/lesson learned", not just plain facts)

- Pitfall root causes: symptom X, why the obvious approach W didn't work, actual root cause Y, solution Z
- Verified non-obvious behaviors: actual behavior of API/library/tool in edge cases that contradicts docs or intuition
- Design decision rationale: why choose A over B under what constraints, what is sacrificed by not choosing B
- Project implicit conventions: cross-module contracts, inviolable invariants, naming/structure rules hard to find via grep
- Environment/deployment/toolchain pitfalls: version combinations, platform differences, CI behaviors
- Rejected approaches and reasons

## Strictly Forbidden

- AI assistant's own operating mechanisms: built-in tool behaviors, system prompt rules, internal mechanisms like memory/hooks/skills
- General programming common sense: content readily available in introductory materials
- Conversation logs: process descriptions of what was done, rather than distilled conclusions
- Statically searchable facts: information immediately obtainable by grep/reading code
- One-time task state: PR numbers, current progress, temporary debugging goals
- Content already recorded in the "Existing Knowledge Base" below

## Self-check Three Questions (run each candidate through)

1. A week from now doing a similar task, what if this entry were missing? If you could figure it out from code/docs → discard
2. Is this about the user's code/project/domain, or about the AI assistant's tools/mechanisms? The latter → always discard
3. Can you phrase it as "Because X, therefore Y / Don't do Z" or "X caused problems, root cause was Y"? If it can only be plain facts → discard

## Existing Knowledge Base

{existing_knowledge}

## Conversation Log

Note: Contains only user questions and assistant final responses, no tool call details. When in doubt, don't extract.

{compressed_session}

## Output

JSON array. **Most cases output []**. Max {max_items} entries.

```json
[
  {{
    "title": "Concise title (10-30 chars, highlighting experience/lesson)",
    "content": "Background + phenomenon + root cause / why + conclusion / approach, with concrete examples, readable independently from conversation context"
  }}
]
```

"""

_PROMPT_AGENT_EN = """\
You are a memory organization assistant. Below is a compressed conversation between a user and an AI assistant.

Please extract information worth remembering long-term. Each memory entry should be readable and usable independently from the original conversation context.

## Extraction Criteria

Worth extracting:
- User's explicitly expressed preferences (response style, language habits, focus areas, etc.)
- User's personal background information (profession, tech stack, team/company, role, etc.)
- Important facts mentioned by the user (project names, deadlines, key people, etc.)
- Domain knowledge or professional topics the user cares about
- Places where the user corrected the assistant (indicating the assistant's previous understanding was wrong)
- Recurring interaction patterns

Not worth extracting:
- One-time transactional requests
- Small talk, acknowledgments, thanks in conversation
- Content already recorded in "Existing Memories" below

## Existing Memories

{existing_knowledge}

## Conversation Log

{compressed_session}

## Output Format

Output a JSON array, each element is an independent memory entry. Output [] if nothing worth extracting. Max {max_items} entries.

```json
[
  {{
    "title": "Concise title (10-30 characters)",
    "content": "Complete memory description in declarative sentences, describing facts, readable independently from conversation context"
  }}
]
```

Requirements: Each memory entry is self-contained. Better to have nothing than to force entries.
"""
