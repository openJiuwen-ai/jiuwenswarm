# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Carry every model call of the research-harness capability pack through JiuwenSwarm.

The capability pack (``code/vendor``: Research Harness and its PaperBanana figure
stack) decides which model to ask, with which prompt, at which tier. This module
changes only the channel. Each text call becomes a single-turn JiuwenSwarm
DeepAgent whose rails see the call: ``UsageLedgerRail`` reads the usage the model
client attached to the response, ``RigorAuditRail`` audits the reply text (audit
only, the prompt is not touched), and ``MultimodalImageRail`` attaches input
images. Image generation, which the framework's OpenAI client does not implement,
goes over the OpenAI images API and is charged to the same ledger.

Every call is appended to ``usage.jsonl`` with its own start and finish time,
and every answer is cached by request in ``bridge_cache.jsonl`` (images as files
in ``bridge_media/``). A replay answers from the cache, charges the ledger with
the rows the run recorded, and stops on an uncached request instead of calling out.

Endpoint, key and model come from the JiuwenSwarm run configuration
(``RESEARCH_CHAT_*`` and ``RESEARCH_IMAGE_*``), so a rerun with another key or
another model needs no change to the capability pack. The model the pack asked
for is kept in each row as ``requested_model``.

    transport = Transport(run_dir, stage="write")
    transport.install()          # Research Harness and PaperBanana now call through it
    with transport.step("polish/review"):
        ...                      # any Research Harness primitive
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import hashlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# code/jiuwenswarm/jiuwenswarm/agents/harness/common/research_bridge/ -> code/vendor
VENDOR = Path(__file__).resolve().parents[6] / "vendor"
VENDOR_PATHS = ("research_harness/packages/research_harness", "llm_router")
CACHE_NAME = "bridge_cache.jsonl"
MEDIA_NAME = "bridge_media"
# The capability pack puts its instructions in the user turn; the framework's general
# assistant prompt would add a few hundred tokens of unrelated text to every call.
DEFAULT_SYSTEM = "You are a research writing assistant. Follow the instructions in the user message exactly."


def add_vendor_paths(vendor: Path | None = None) -> Path:
    """Put the capability pack on ``sys.path``; ``JIUWENSWARM_VENDOR`` overrides its location."""
    root = Path(vendor or os.environ.get("JIUWENSWARM_VENDOR") or VENDOR).resolve()
    for rel in VENDOR_PATHS:
        p = str(root / rel)
        if p not in sys.path:
            sys.path.insert(0, p)
    return root


def chat_endpoint() -> tuple[str, str]:
    env = os.environ.get
    return (env("RESEARCH_CHAT_BASE_URL") or env("OPENAI_BASE_URL", ""),
            env("RESEARCH_CHAT_API_KEY") or env("OPENAI_API_KEY", ""))


def build_model(model: str, temperature: float | None = None, base: str = "", key: str = ""):
    """A JiuwenSwarm ``Model`` on the configured OpenAI-compatible endpoint."""
    from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
    base, key = (base, key) if base and key else chat_endpoint()
    if not base or not key:
        raise RuntimeError("RESEARCH_CHAT_BASE_URL / RESEARCH_CHAT_API_KEY are not set")
    request_config = {"model": model}
    if temperature is not None:
        request_config["temperature"] = temperature
    return Model(model_client_config=ModelClientConfig(client_provider="OpenAI", api_key=key, api_base=base,
                                                      timeout=900, verify_ssl=False),
                 model_config=ModelRequestConfig(**request_config))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _run_sync(coro):
    """Run a coroutine from synchronous code, also when the caller is already inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _strip_provider(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


class Transport:
    """The channel: JiuwenSwarm model calls with rails, a per-call ledger and a request cache."""

    def __init__(self, run_dir: Path, *, replay: bool | None = None, stage: str = "write",
                 on_usage: Callable[[dict], None] = lambda row: None) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.run_dir / CACHE_NAME
        self.media = self.run_dir / MEDIA_NAME
        self.ledger = self.run_dir / "usage.jsonl"
        # RESEARCH_BRIDGE_REPLAY=1 turns any driver script into a replay without editing it
        self.replay = bool(os.environ.get("RESEARCH_BRIDGE_REPLAY")) if replay is None else replay
        self.stage = stage
        self._step = ""
        self.on_usage = on_usage
        self.rows: list[dict] = []  # rows charged by this transport, in order
        self.rigor_findings: list[str] = []
        self._lock = threading.Lock()
        self.calls = len(self.ledger.read_text(encoding="utf-8").splitlines()) if self.ledger.exists() else 0
        self.cache: dict[str, dict] = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.cache[rec["key"]] = rec
        env = os.environ.get
        self.chat_model = env("RESEARCH_CHAT_MODEL", "")
        self.image_model = env("RESEARCH_IMAGE_MODEL", "")
        self._chat_ep = chat_endpoint()
        self._image_ep = (env("RESEARCH_IMAGE_BASE_URL") or self._chat_ep[0],
                          env("RESEARCH_IMAGE_API_KEY") or self._chat_ep[1])

    # -- bookkeeping ------------------------------------------------------------------------

    @contextlib.contextmanager
    def step(self, name: str, stage: str | None = None):
        """Label the calls made inside the block (``usage.jsonl`` columns ``stage`` and ``step``)."""
        saved = self.stage, self._step
        self.stage, self._step = stage or self.stage, name
        try:
            yield self
        finally:
            self.stage, self._step = saved

    def _media_file(self, data: bytes, ext: str) -> Path:
        self.media.mkdir(parents=True, exist_ok=True)
        path = self.media / f"{hashlib.sha256(data).hexdigest()[:16]}.{ext}"
        if not path.exists():
            path.write_bytes(data)
        return path

    async def _acached(self, request: dict, call) -> dict:
        """The cached answer to ``request``; ``call`` (a coroutine factory) runs only on a miss."""
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        rec = self.cache.get(key)
        if rec is None:
            if self.replay:
                raise SystemExit(f"--replay: request {key[:12]} ({self.stage}/{self._step}) is not cached")
            started, t0 = _now(), time.monotonic()
            answer, rows = await call()
            finished, latency = _now(), round(time.monotonic() - t0, 3)
            rows = [dict(r, started_at=started, ts=finished, latency_s=latency) for r in rows]
            rec = {"key": key, "kind": request["kind"], **answer, "usage": rows}
            with self._lock:
                with self.cache_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self.cache[key] = rec
        self._charge(rec, key)
        return rec

    def _charge(self, rec: dict, key: str) -> None:
        with self._lock:
            for row in rec["usage"]:
                self.calls += 1
                out = dict(row, stage=self.stage, step=self._step, seq=self.calls, origin="jiuwenswarm",
                           replayed=self.replay, source_record=f"{CACHE_NAME}#{key[:16]}")
                with self.ledger.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(out, ensure_ascii=False) + "\n")
                self.rows.append(out)
                self.on_usage(out)
            self.rigor_findings += rec.get("rigor_findings", [])

    # -- text -------------------------------------------------------------------------------

    def _model_for(self, requested: str) -> str:
        return self.chat_model or _strip_provider(requested)

    def _image_files(self, images: list[tuple[bytes, str]]) -> list[dict]:
        files = []
        for data, mime in images:
            path = self._media_file(data, mime.split("/")[-1].replace("jpeg", "jpg"))
            files.append({"type": "image", "path": str(path), "mime_type": mime})
        return files

    async def _agent_call(self, model: str, requested: str, system: str, prompt: str, temperature: float | None,
                          images: list[tuple[bytes, str]]):
        from openjiuwen.core.single_agent import AgentCard
        from openjiuwen.harness.factory import create_deep_agent

        from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
            reset_current_multimodal_image_files, set_current_multimodal_image_files)
        from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import MultimodalImageRail
        from jiuwenswarm.agents.harness.team.rails.rigor_audit_rail import RigorAuditRail
        from jiuwenswarm.agents.harness.team.rails.usage_ledger_rail import UsageLedgerRail

        llm = build_model(model, temperature, *self._chat_ep)
        rows: list[dict] = []
        rigor = RigorAuditRail(language="en", inject_prompt=False)
        rails = [rigor, UsageLedgerRail(rows.append, stage=self.stage, step=self._step)]
        token = None
        if images:
            rails.append(MultimodalImageRail(enable_image_multimodal=True))
            token = set_current_multimodal_image_files(self._image_files(images))
        last = None
        try:
            for attempt in range(3):  # a dropped gateway connection is not the model's answer
                rows.clear()
                agent = create_deep_agent(llm, card=AgentCard(name="rh_bridge", id="rh_bridge"),
                                          system_prompt=system or DEFAULT_SYSTEM, tools=[], rails=rails,
                                          max_iterations=1, workspace=str(self.run_dir / "agent_workspace"),
                                          add_general_purpose_agent=False, enable_llm_retry_rail=False,
                                          enable_read_image_multimodal=False)
                try:
                    out = await agent.invoke({"query": prompt})
                    text = (out or {}).get("output", "") if isinstance(out, dict) else str(out or "")
                    if text.strip():
                        break
                    last = RuntimeError("empty answer")
                except Exception as exc:  # noqa: BLE001 - the framework wraps transport errors
                    last = exc
                if attempt == 2:
                    raise RuntimeError(f"bridge chat failed after 3 attempts: {last}") from last
                await asyncio.sleep(20 * (attempt + 1))
        finally:
            if token is not None:
                reset_current_multimodal_image_files(token)
        for row in rows:
            row.update(kind="chat", model=row.get("model") or model, requested_model=requested)
        return {"text": text, "rigor_findings": [f.render() for f in rigor.findings]}, rows

    def _chat_request(self, model, system, prompt, temperature, images) -> dict:
        return {"kind": "chat", "model": model, "system": system, "prompt": prompt, "temperature": temperature,
                "images": [hashlib.sha256(d).hexdigest() for d, _ in images]}

    def chat(self, prompt: str, *, system: str = "", requested_model: str = "", temperature: float | None = None,
             images: list[tuple[bytes, str]] = ()) -> str:
        return _run_sync(self.achat(prompt, system=system, requested_model=requested_model,
                                    temperature=temperature, images=images))

    async def achat(self, prompt: str, *, system: str = "", requested_model: str = "",
                    temperature: float | None = None, images: list[tuple[bytes, str]] = ()) -> str:
        model = self._model_for(requested_model)
        images = list(images)
        rec = await self._acached(self._chat_request(model, system, prompt, temperature, images),
                                  lambda: self._agent_call(model, requested_model, system, prompt, temperature, images))
        return rec["text"]

    # -- images -----------------------------------------------------------------------------

    async def apaint(self, prompt: str, *, size: str, requested_model: str = "",
                     images: list[tuple[bytes, str]] = ()) -> bytes:
        """A generated image (PNG bytes); with input images the edits endpoint is used."""
        model = self.image_model or _strip_provider(requested_model)
        images = list(images)
        request = {"kind": "image", "model": model, "prompt": prompt, "size": size,
                   "images": [hashlib.sha256(d).hexdigest() for d, _ in images]}

        async def call():
            from jiuwenswarm.agents.harness.common.research.gateway import _post

            base, key = self._image_ep
            body = {"model": model, "prompt": prompt, "n": 1, "size": size, "response_format": "b64_json"}
            if images:
                body["images"] = [{"image_url": f"data:{m};base64,{base64.b64encode(d).decode()}"} for d, m in images]
            path = "/images/edits" if images else "/images/generations"
            data = await asyncio.to_thread(_post, base.rstrip("/") + path, key, body)
            item = data["data"][0]
            raw = item.get("b64_json") or (item.get("url") or "").split(",", 1)[-1]
            png = self._media_file(base64.b64decode(raw), "png")
            usage = data.get("usage") or {}
            row = {"kind": "image", "model": model, "requested_model": requested_model, "input_tokens": int(usage.get("input_tokens") or 0),
                   "output_tokens": int(usage.get("output_tokens") or 0),
                   "total_tokens": int(usage.get("total_tokens") or 0), "measured": bool(usage)}
            return {"image": f"{MEDIA_NAME}/{png.name}"}, [row]

        rec = await self._acached(request, call)
        return (self.run_dir / rec["image"]).read_bytes()

    # -- installation -----------------------------------------------------------------------

    def install(self, *, research_harness: bool = True, paperbanana: Path | None = None) -> None:
        """Route the capability pack's calls through this transport.

        Research Harness: its writing-chain primitives reach a model through
        ``exemplar_impls._chat``, which falls back to ``_direct_chat`` under
        ``RH_EAG_LLM=openai_direct``; that function is replaced, so every caller
        (EAG, polish, logic, unify, de-style) goes through the bridge. RH's own
        usage report keeps working because the replacement books each call there too.

        PaperBanana (``paperbanana`` = its directory): its text funnel
        ``call_model_with_retry_async`` and its GPT Image funnel are replaced, and
        both pipeline stages are pointed at the GPT Image route.
        """
        if research_harness:
            add_vendor_paths()
            from research_harness.primitives import exemplar_impls

            os.environ["RH_EAG_LLM"] = "openai_direct"

            def _direct_chat(model: str, prompt: str) -> str:
                before = len(self.rows)
                text = self.chat(prompt, requested_model=model, temperature=0.3)
                for row in self.rows[before:]:
                    exemplar_impls._book(row["model"], row["input_tokens"], row["output_tokens"])
                return text

            exemplar_impls._direct_chat = _direct_chat
        if paperbanana is not None:
            pb = str(Path(paperbanana).resolve())
            if pb not in sys.path:
                sys.path.insert(0, pb)
            os.environ.update(DRAFT_IMAGE_PROVIDER="gpt_image", FINAL_IMAGE_PROVIDER="gpt_image")
            from utils import generation_utils as gu

            async def call_model(model_name, contents, config, max_attempts=5, retry_delay=5, error_context=""):
                system, temperature, n = _genai_config(config)
                prompt = "\n\n".join(c["text"] for c in contents if c.get("type") == "text" and c.get("text"))
                imgs = _content_images(contents)
                return [await self.achat(prompt, system=system, requested_model=model_name,
                                         temperature=temperature, images=imgs) for _ in range(n)]

            async def call_image(model_name, contents, config, max_attempts=5, retry_delay=30, error_context=""):
                aspect = gu._normalize_aspect_ratio(config.get("aspect_ratio", "1:1"))
                size = config.get("size") or gu.get_openai_image_size_for_aspect_ratio(
                    aspect, stage=config.get("stage", "draft"))
                png = await self.apaint(gu._contents_to_text_prompt(contents)[:30000], size=size,
                                        requested_model=gu.get_gpt_image_model(model_name),
                                        images=_content_images(contents)[:5])
                return [base64.b64encode(png).decode()]

            gu.call_model_with_retry_async = call_model
            gu.call_gpt_image_generation_with_retry_async = call_image
            gu._has_gpt_image_configured = lambda: True
            gu.is_gpt_image_configured = lambda: True


def _genai_config(config: Any) -> tuple[str, float | None, int]:
    get = config.get if isinstance(config, dict) else lambda k, d=None: getattr(config, k, d)
    system = get("system_instruction") or get("system_prompt") or ""
    if not isinstance(system, str):  # google-genai may hold a Content object
        system = "".join(getattr(p, "text", "") or "" for p in getattr(system, "parts", []) or [])
    n = get("candidate_count") or get("candidate_num") or 1
    return system, get("temperature"), int(n)


def _content_images(contents: list[dict]) -> list[tuple[bytes, str]]:
    out = []
    for c in contents:
        if c.get("type") != "image":
            continue
        src = c.get("source") if isinstance(c.get("source"), dict) else {}
        b64 = c.get("image_base64") or src.get("data") or c.get("data") or ""
        mime = src.get("media_type") or c.get("mime_type") or ("image/jpeg" if c.get("image_base64") else "image/png")
        if b64:
            out.append((base64.b64decode(b64), mime))
    return out


__all__ = ["Transport", "add_vendor_paths", "VENDOR"]
