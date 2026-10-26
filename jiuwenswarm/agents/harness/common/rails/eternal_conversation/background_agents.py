"""Independent DeepAgent runners for Persist Session background workers."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openjiuwen.core.foundation.llm.schema.message import SystemMessage, UserMessage
from openjiuwen.core.session.agent import create_agent_session

from .background_specs import BackgroundBuildContext, build_background_deep_agent_spec
from .evidence import EvidenceWriter, jsonable, write_json_atomic
from .memory_cli import VENDORED_SKILL
from .prompts import render_extractor_fork_user_prompt


MAX_BACKGROUND_ATTEMPTS = 6
MAX_RETRY_RESPONSE_CHARS = 16_000
BACKGROUND_INVOCATION_TIMEOUT_SECONDS = 600.0

EXTRACTOR_RETRY_CHECKLIST = """
Rebuild the proposal draft from scratch through sequential write_file operation calls to the exact
proposal path in extractor_run. Do not write or return one complete proposal JSON.
- inspect the staged memory with dynamic-memory-cli before deciding;
- add every Snapshot item separately with action=add_snapshot_item, respecting 4/4/6/4/4/6;
- add every UT separately with action=add_ut; use at most four, content at most 650 characters,
  1-4 queries, 1-3 verbatim must_include strings, and priority 20/40/60/80/100;
- add exactly one assessment per candidate with action=add_assessment and field ut_id;
- unresolved assessments copy snapshot_notice into a separately added snapshot.constraints item;
- overridden assessments cite direct-user cursor refs and exact acknowledgement quotes;
- after all entries are accepted, call action=finalize with semantic_statement;
- after finalize succeeds, invoke no more tools and return only {"status":"finalized"}.
Each tool failure is plain-text feedback for that one entry. Correct it before continuing. Do not add
Markdown fences or commentary.
""".strip()

BUILDER_RETRY_CHECKLIST = """
Rebuild the Builder's final answer from scratch and return exactly one strict JSON object.
- If run_builder.py produced a manifest whose top-level success is true, return approved=true.
- A build-pending result may list UTs under skipped after an idempotent rerun; this is success when
  the manifest says success=true and test --built-only plus bench both returned zero.
- Do not inspect SQLite, compare timestamps or document bytes, reinterpret UT semantics, or run
  private diagnostic scripts. Those checks belong to dynamic-memory-cli and the Harness.
- After a successful helper result, invoke no more tools; immediately return approved JSON with the
  workspace-relative build_manifest path from builder_run.
- Use double-quoted JSON strings, JSON-escape Windows path backslashes, and add no Markdown fences
  or commentary outside the JSON object.
""".strip()


def _content_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("output", "content", "response", "result"):
            if key in value:
                return _content_text(value[key])
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("background Agent did not return a JSON object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("background Agent JSON must be an object")
    return value


@dataclass
class _DeepAgentRuntime:
    model_identity: str
    agent: Any
    context: BackgroundBuildContext


@dataclass(frozen=True)
class ExtractorForkContext:
    """One immutable, model-visible Worker prefix for an Extractor fork."""

    system_prompt: str
    messages: tuple[Any, ...]
    tools: tuple[Any, ...]
    parent_session_id: str
    source_input_tokens: int | None
    context_window_tokens: int | None
    prefix_sha256: str
    kv_cache_affinity_config: Any = None

    @property
    def extraction_boundary_reached(self) -> bool:
        """Report the 70% extraction trigger without making it a fork limit."""
        if self.source_input_tokens is None or not self.context_window_tokens:
            return False
        return self.source_input_tokens >= int(self.context_window_tokens * 0.70)

    def manifest(self) -> dict[str, Any]:
        ratio = None
        if self.source_input_tokens is not None and self.context_window_tokens:
            ratio = self.source_input_tokens / self.context_window_tokens
        return {
            "parent_session_id": self.parent_session_id,
            "source_input_tokens": self.source_input_tokens,
            "context_window_tokens": self.context_window_tokens,
            "context_window_ratio": ratio,
            "prefix_sha256": self.prefix_sha256,
            "message_count": len(self.messages) + 1,
            "tool_count": len(self.tools),
        }


class BackgroundAgentRunner:
    """Run Extractor and Builder through role-specific DeepAgentSpec instances.

    The small direct-model compatibility path exists only for unit-test doubles
    that are not openJiuwen ``Model`` objects. Product models always go through
    ``DeepAgentSpec.build`` and the inner ReAct loop.
    """

    def __init__(
        self,
        model_supplier: Callable[[], Any],
        evidence: EvidenceWriter,
        *,
        root: Path | None = None,
        session_id: str | None = None,
    ) -> None:
        self._model_supplier = model_supplier
        self._evidence = evidence
        self._root = Path(root or evidence.root)
        self._session_id = str(session_id or evidence.session_id)
        self._runtimes: dict[str, _DeepAgentRuntime] = {}

    def set_model_supplier(self, model_supplier: Callable[[], Any]) -> None:
        """Rebind after a channel recreates its session-scoped Adapter."""
        self._model_supplier = model_supplier

    @staticmethod
    def _is_openjiuwen_model(model: Any) -> bool:
        return hasattr(model, "model_client_config") and hasattr(model, "model_config")

    def workspace_for(self, role: str) -> Path:
        """Return sibling role workspaces under the session's background job."""
        return self._root / "jobs" / "eternal" / role

    def uses_deep_agent(self) -> bool:
        """Whether the currently selected product model can build from ModelSpec."""
        return self._is_openjiuwen_model(self._model_supplier())

    def prepare_workspace(self, role: str) -> Path:
        """Create one role workspace and stage only its declared Skills."""
        workspace = self.workspace_for(role)
        workspace.mkdir(parents=True, exist_ok=True)
        self._stage_skill(workspace)
        return workspace

    @staticmethod
    def _stage_skill(workspace: Path) -> None:
        skills_root = workspace / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        if not VENDORED_SKILL.is_dir():
            raise FileNotFoundError(
                f"Persist Session dynamic-memory-cli Skill is missing: {VENDORED_SKILL}"
            )
        shutil.copytree(
            VENDORED_SKILL,
            skills_root / VENDORED_SKILL.name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    @staticmethod
    def _model_identity(model: Any) -> str:
        """Fingerprint the selected foreground model without hard-coding a name."""
        payload = {
            "client": jsonable(getattr(model, "model_client_config", None)),
            "request": jsonable(getattr(model, "model_config", None)),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def _ensure_runtime(
        self,
        role: str,
        system_prompt: str,
        model: Any,
        fork_context: ExtractorForkContext | None = None,
    ) -> _DeepAgentRuntime:
        model_identity = self._model_identity(model)
        if fork_context is not None:
            model_identity = f"{model_identity}:{fork_context.prefix_sha256}"
        current = self._runtimes.get(role)
        if current is not None and current.model_identity == model_identity:
            return current
        if current is not None:
            stop = getattr(current.agent, "stop", None)
            if stop is not None:
                await stop()
        workspace = self.prepare_workspace(role)
        spec, context = build_background_deep_agent_spec(
            role=role,
            model=model,
            session_id=self._session_id,
            workspace_root=workspace,
            system_prompt=system_prompt,
            fork_tools=(list(fork_context.tools) if fork_context is not None else None),
            fork_kv_cache_affinity_config=(
                fork_context.kv_cache_affinity_config
                if fork_context is not None
                else None
            ),
        )
        if not isinstance(context, BackgroundBuildContext):
            raise TypeError("invalid Persist Session background build context")
        context.evidence = self._evidence
        if fork_context is not None:
            context.fork_prefix_sha256 = fork_context.prefix_sha256
            context.fork_prefix_message_count = len(fork_context.messages) + 1
            context.fork_system_prompt = fork_context.system_prompt
        agent = spec.build(context)
        runtime = _DeepAgentRuntime(model_identity, agent, context)
        self._runtimes[role] = runtime
        return runtime

    async def _discard_runtime(self, role: str, runtime: _DeepAgentRuntime) -> None:
        """Retire a timed-out runtime without waiting indefinitely for I/O."""
        if self._runtimes.get(role) is runtime:
            self._runtimes.pop(role, None)
        stop = getattr(runtime.agent, "stop", None)
        if stop is None:
            return
        stop_task = asyncio.create_task(stop())
        done, _ = await asyncio.wait({stop_task}, timeout=5.0)
        if not done:
            stop_task.cancel()
            stop_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )

    async def _invoke(
        self,
        *,
        role: str,
        system_prompt: str,
        prompt: str,
        model: Any,
        fork_context: ExtractorForkContext | None = None,
        proposal_path: Path | None = None,
        proposal_validator: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[str, Any, dict[str, Any]]:
        if not self._is_openjiuwen_model(model):
            # Existing unit tests use intentionally tiny model doubles. They
            # cannot be reconstructed by ModelSpec, so retain only this test
            # seam; no product model reaches it.
            response = await model.invoke(
                [SystemMessage(content=system_prompt), UserMessage(content=prompt)],
                temperature=0,
            )
            return (
                _content_text(response),
                jsonable(getattr(response, "usage_metadata", None)),
                {},
            )
        effective_system_prompt = (
            fork_context.system_prompt if fork_context is not None else system_prompt
        )
        runtime = await self._ensure_runtime(
            role,
            effective_system_prompt,
            model,
            fork_context,
        )
        # Each frozen job is self-contained, so carrying an inner Agent Session
        # into the next job only duplicates old prompts and grows without bound.
        # Full role histories remain in EvidenceWriter; the execution session is
        # deliberately fresh for every invocation.
        conversation_id = f"{self._session_id}:persist:{role}"
        execution_session_id = f"{conversation_id}:{uuid.uuid4().hex}"
        if fork_context is not None:
            await runtime.agent.create_new_context_engine(
                session_id=execution_session_id,
                messages=[copy.deepcopy(item) for item in fork_context.messages],
            )
        invocation_session = create_agent_session(
            session_id=execution_session_id,
            card=runtime.agent.card,
            parent_session_id=(
                fork_context.parent_session_id if fork_context is not None else None
            ),
        )
        runtime.context.model_usages.clear()
        runtime.context.fork_prefix_checks.clear()
        runtime.context.extractor_tool_calls.clear()
        runtime.context.extractor_proposal_validation.configure(
            proposal_path if role == "extractor" else None,
            proposal_validator if role == "extractor" else None,
        )
        try:
            invoke_task = asyncio.create_task(
                runtime.agent.invoke(
                    {
                        "query": prompt,
                        "conversation_id": (
                            execution_session_id
                            if fork_context is not None
                            else conversation_id
                        ),
                    },
                    session=invocation_session,
                )
            )
            done, _ = await asyncio.wait(
                {invoke_task}, timeout=BACKGROUND_INVOCATION_TIMEOUT_SECONDS
            )
            if not done:
                invoke_task.cancel()
                invoke_task.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )
                await self._discard_runtime(role, runtime)
                raise TimeoutError(
                    f"Persist Session {role} Agent exceeded "
                    f"{BACKGROUND_INVOCATION_TIMEOUT_SECONDS:g}s"
                )
            result = invoke_task.result()
        finally:
            runtime.context.extractor_proposal_validation.clear()
        usage: Any = None
        if runtime.context.model_usages:
            usage = list(runtime.context.model_usages)
        if isinstance(result, dict):
            usage = usage or jsonable(result.get("usage") or result.get("usage_metadata"))
        prefix_check = (
            dict(runtime.context.fork_prefix_checks[0])
            if runtime.context.fork_prefix_checks
            else {}
        )
        if fork_context is not None:
            prefix_check["extractor_tool_calls"] = copy.deepcopy(
                runtime.context.extractor_tool_calls
            )
        return _content_text(result), usage, prefix_check

    async def call_json(
        self,
        *,
        role: str,
        system_prompt: str,
        request: dict[str, Any],
        validate: Callable[[dict[str, Any]], None] | None = None,
        fork_context: ExtractorForkContext | None = None,
    ) -> dict[str, Any]:
        model = self._model_supplier()
        if model is None:
            raise RuntimeError("eternal-conversation background model is unavailable")
        workspace = self.workspace_for(role)
        workspace.mkdir(parents=True, exist_ok=True)
        extractor_run = request.get("extractor_run")
        if not isinstance(extractor_run, dict):
            extractor_run = None
        request_relative = str(
            (extractor_run or {}).get("request") or "input/latest-request.json"
        )
        proposal_relative = str(
            (extractor_run or {}).get("proposal") or "output/proposal.json"
        )
        staged_memory_relative = str(
            (extractor_run or {}).get("memory_root") or "staged-memory"
        )
        request_path = workspace / request_relative
        proposal_path = workspace / proposal_relative
        request_json = json.dumps(request, ensure_ascii=False)
        original_prompt = request_json
        if fork_context is not None:
            fork_prompt = render_extractor_fork_user_prompt(
                extractor_workspace=str(workspace.resolve()),
                staged_memory_root=staged_memory_relative,
                request_path=request_relative,
                proposal_path=proposal_relative,
            )
            original_prompt = f"{fork_prompt}\n{request_json}"
        await asyncio.to_thread(
            write_json_atomic,
            request_path,
            request,
        )
        if extractor_run is not None and proposal_path.exists():
            proposal_path.unlink()
        prompt = original_prompt
        last_error: Exception | None = None
        for attempt in range(1, MAX_BACKGROUND_ATTEMPTS + 1):
            response_text: str | None = None
            usage: Any = None
            try:
                response_text, usage, first_prefix_check = await self._invoke(
                    role=role,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    model=model,
                    fork_context=fork_context,
                    proposal_path=(proposal_path if extractor_run is not None else None),
                    proposal_validator=validate,
                )
                if fork_context is not None:
                    first_usage = (
                        usage[0]
                        if isinstance(usage, list) and usage
                        else usage if isinstance(usage, dict) else {}
                    )
                    input_tokens = int(first_usage.get("input_tokens") or 0)
                    cache_tokens = int(
                        first_usage.get("cache_read_tokens")
                        or first_usage.get("cache_tokens")
                        or 0
                    )
                    source_tokens = int(fork_context.source_input_tokens or 0)
                    byte_prefix_match = first_prefix_check.get("matches") is True
                    prefix_hit_rate = (
                        min(cache_tokens / source_tokens, 1.0)
                        if source_tokens > 0
                        else None
                    )
                    await self._evidence.append_audit(
                        "extractor-fork-cache",
                        {
                            **fork_context.manifest(),
                            "extractor_input_tokens": input_tokens,
                            "extractor_cache_read_tokens": cache_tokens,
                            "fork_prefix_hit_rate": prefix_hit_rate,
                            "byte_prefix_match": byte_prefix_match,
                            "actual_prefix_sha256": first_prefix_check.get(
                                "actual_sha256"
                            ),
                            "full_prefix_hit": (
                                byte_prefix_match and cache_tokens >= source_tokens
                                if source_tokens > 0
                                else None
                            ),
                        },
                    )
                if extractor_run is not None:
                    if not proposal_path.is_file():
                        raise ValueError(
                            f"Extractor did not write the required proposal: {proposal_relative}"
                        )
                    parsed = json.loads(proposal_path.read_text(encoding="utf-8"))
                    if not isinstance(parsed, dict):
                        raise ValueError("Extractor proposal file must contain one JSON object")
                    if validate is not None:
                        validate(parsed)
                    tool_calls = list(
                        first_prefix_check.get("extractor_tool_calls") or []
                    )
                    if not any(
                        item.get("kind") == "dynamic-memory-cli"
                        for item in tool_calls
                        if isinstance(item, dict)
                    ):
                        raise ValueError(
                            "Extractor must inspect staged memory through dynamic-memory-cli"
                        )
                    if not any(
                        item.get("tool") == "write_file"
                        for item in tool_calls
                        if isinstance(item, dict)
                    ):
                        raise ValueError(
                            "Extractor must save its proposal with write_file"
                        )
                else:
                    parsed = _json_object(response_text)
                    if validate is not None:
                        validate(parsed)
                await self._evidence.append_agent_history(
                    role,
                    {
                        "attempt": attempt,
                        "system_prompt": system_prompt,
                        "request": request,
                        "response": response_text,
                        "usage": usage,
                        "status": "accepted",
                    },
                )
                return parsed
            except Exception as exc:
                last_error = exc
                if extractor_run is not None and proposal_path.exists():
                    proposal_path.unlink()
                await self._evidence.append_agent_history(
                    role,
                    {
                        "attempt": attempt,
                        "system_prompt": system_prompt,
                        "request": request,
                        "response": response_text,
                        "usage": usage,
                        "status": "rejected",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                prompt = (
                    original_prompt
                    + "\n\nYour previous response was invalid. Here is the exact previous response"
                    + " (possibly truncated):\n"
                    + (response_text or "<no response>")[:MAX_RETRY_RESPONSE_CHARS]
                    + "\n\nValidation error: "
                    + str(exc)
                    + "\n"
                    + (
                        BUILDER_RETRY_CHECKLIST
                        if role == "builder"
                        else EXTRACTOR_RETRY_CHECKLIST
                    )
                )
        if last_error is None:
            raise RuntimeError("background Agent exhausted retries without an error")
        raise last_error

    async def close(self) -> None:
        """Stop any materialized DeepAgent interaction runtimes."""
        for runtime in list(self._runtimes.values()):
            stop = getattr(runtime.agent, "stop", None)
            if stop is not None:
                await stop()
        self._runtimes.clear()


__all__ = ["BackgroundAgentRunner", "ExtractorForkContext"]
