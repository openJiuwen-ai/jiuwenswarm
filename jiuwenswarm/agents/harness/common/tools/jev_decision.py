# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Client for Jev, TypeSafe's decision model, and the rule for which steps use it.

Jev does not write text. Given a piece of text (the *state*) it answers typed
questions about it in one call: `noul` returns the probability of yes, `choice`
picks one option and gives a probability for every option, `score` places the
state on an ordered scale. It is fast and cheap -- input tokens are billed,
output tokens are not -- so it takes the research steps that are decisions:
is this paper relevant, is this sentence a finding, does this claim support
or challenge the question, does the study design meet a checklist item.
Drafting and extraction stay on a chat model. `route_for()` is that rule.

Wire format, from the official quick start:

    POST {base}/v1/systemone
    Authorization: Bearer <key>
    {"model": "jev-latest", "state": ..., "questions": {key: {"type": ..., ...}}}
    -> {"model": "jev-1.13.0", "answers": {key: {...}}, "usage": {"input_tokens", "output_tokens"}}

429 and 529 are retried with exponential backoff; 401 and 422 are errors the
caller has to fix. Every request and response can be kept in a cache file keyed
by the request hash, so a run can be replayed exactly without a key.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
INPUT_USD_PER_MTOK = 0.042  # output tokens are not billed
MAX_OPTIONS = 255

# Research steps that are decisions over a given text go to Jev; the rest go to
# a chat model. Kept as data so a reader can see the whole routing at once.
DECISION_TASKS = frozenset({
    "relevance_screen",   # is this paper evidence for the research question
    "finding_check",      # is this sentence a reported finding
    "evidence_stance",    # how does this finding bear on the question
    "open_question",      # is the question still open given the evidence
    "design_checklist",   # does the study design meet this item
})


def route_for(task: str) -> str:
    """'jev' for a decision step, 'chat' for anything that has to write text."""
    return "jev" if task in DECISION_TASKS else "chat"


def noul(instructions: str) -> dict:
    return {"type": "noul", "instructions": instructions}


def choice(instructions: str, criteria: dict[str, str | None]) -> dict:
    if not 2 <= len(criteria) <= MAX_OPTIONS:
        raise ValueError(f"a choice needs 2..{MAX_OPTIONS} options, got {len(criteria)}")
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions: str, tiers: list[str]) -> dict:
    if len(tiers) < 2:
        raise ValueError("a score needs at least two tiers")
    return {"type": "score", "instructions": instructions, "criteria": tiers}


class JevError(RuntimeError):
    """A request Jev will not answer as sent (bad key, invalid body, out of retries)."""


def _http_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def request_key(model: str, state: Any, questions: dict) -> str:
    """Hash of one request, used as its cache key."""
    blob = json.dumps({"model": model, "state": state, "questions": questions},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class JevClient:
    """Send decision questions to Jev, with retries, usage accounting and a replay cache.

    `cache_path` is a JSONL file of {"key", "request", "response", "ts"}. With
    `replay=True` every answer must come from that file and nothing is sent, so
    a reviewer without a key can reproduce a run exactly.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
        max_retries: int = 4,
        cache_path: Path | None = None,
        replay: bool = False,
        post: Callable[[str, bytes, dict[str, str], float], tuple[int, str]] = _http_post,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY", "")
        self.base_url = (base_url or os.environ.get("JEV_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.replay = replay
        self._post = post
        self._sleep = sleep
        self._cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, dict] = {}
        if self._cache_path and self._cache_path.exists():
            for line in self._cache_path.read_text(encoding="utf-8").splitlines():
                if line:
                    rec = json.loads(line)
                    self._cache[rec["key"]] = rec["response"]
        if not replay and not self.api_key:
            raise JevError("no key: set TYPESAFE_API_KEY or JEV_API_KEY, or use replay=True")

    def decide(self, state: Any, questions: dict) -> dict:
        """Answer every question against one state. Returns model, answers and usage."""
        key = request_key(self.model, state, questions)
        if key in self._cache:
            return self._cache[key]
        if self.replay:
            raise JevError(f"request {key[:12]} is not in the replay cache")
        body = json.dumps({"model": self.model, "state": state, "questions": questions}).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/v1/systemone"
        for attempt in range(self.max_retries + 1):
            status, text = self._post(url, body, headers, self.timeout)
            if status == 200:
                response = json.loads(text)
                self._remember(key, state, questions, response)
                return response
            if status in (429, 529) and attempt < self.max_retries:
                self._sleep(2 ** attempt)
                continue
            if status == 401:
                raise JevError("key rejected (401)")
            raise JevError(f"Jev returned {status}: {text[:300]}")
        raise JevError("out of retries")

    def _remember(self, key: str, state: Any, questions: dict, response: dict) -> None:
        self._cache[key] = response
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cache_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "key": key,
                "ts": datetime.now(timezone.utc).isoformat(),
                "request": {"model": self.model, "state": state, "questions": questions},
                "response": response,
            }, ensure_ascii=False) + "\n")


def usage_row(response: dict, *, stage: str, step: str, source_record: str) -> dict:
    """One usage row in JiuwenSwarm's token keys for one Jev call."""
    usage = response.get("usage") or {}
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    return {
        "ts": None,
        "stage": stage,
        "step": step,
        "model": response.get("model", ""),
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "cache_tokens": 0,
        "cost_usd": round(inp * INPUT_USD_PER_MTOK / 1_000_000, 10),
        "success": True,
        "measured": True,
        "source_runtime": "jiuwenswarm",
        "source_record": source_record,
    }
