# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A chat and image client whose every answer is cached, so a replay needs no network.

Answers are cached by request in a JSONL file (images as PNG files in a
directory beside it). A replay that meets an uncached request stops rather
than calling out. Each answer's usage, cached or fresh, is passed to
`on_usage` the first time the run asks for it, so a replay charges the run log
exactly as the run did.

Settings come from the environment, all in the OpenAI wire format
(/chat/completions, /images/generations):

  RESEARCH_CHAT_BASE_URL, RESEARCH_CHAT_API_KEY, RESEARCH_CHAT_MODEL
  RESEARCH_IMAGE_BASE_URL, RESEARCH_IMAGE_API_KEY, RESEARCH_IMAGE_MODEL, RESEARCH_IMAGE_SIZE

Base URLs and keys fall back to OPENAI_BASE_URL and OPENAI_API_KEY.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


RETRYABLE = frozenset({429, 500, 502, 503, 504})


def fetch(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 600,
          attempts: int = 4) -> bytes:
    """The response body. Rate limits, server errors and dropped connections are retried
    with backoff; any other HTTP error is raised at once, the last failure as itself."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE or attempt == attempts - 1:
                raise
            wait = float(exc.headers.get("Retry-After") or 0) if exc.headers else 0.0
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            if attempt == attempts - 1:
                raise
            wait = 0.0
        time.sleep(max(wait, 15.0 * (attempt + 1)))
    raise AssertionError("unreachable")


def _post(url: str, key: str, body: dict, timeout: int = 600) -> dict:
    try:
        return json.loads(fetch(url, json.dumps(body).encode(), timeout=timeout,
                                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{url}: HTTP {exc.code} {exc.read()[:300]!r}") from exc


class CachedGateway:
    """Chat and image calls, cached by request."""

    def __init__(self, cache_path: Path, replay: bool, on_usage: Callable[[dict], None] = lambda row: None) -> None:
        self.cache_path = cache_path
        self.image_dir = cache_path.with_suffix("")
        self.replay = replay
        self.on_usage = on_usage
        self.cache: dict[str, dict] = {}
        self.charged: set[str] = set()
        if cache_path.exists():
            for line in cache_path.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                self.cache[rec["key"]] = rec
        env = os.environ.get
        self.chat_model = env("RESEARCH_CHAT_MODEL", "gpt-5.5")
        self.image_model = env("RESEARCH_IMAGE_MODEL", "gpt-image-1")
        self.image_size = env("RESEARCH_IMAGE_SIZE", "1536x1024")
        self._chat = (env("RESEARCH_CHAT_BASE_URL") or env("OPENAI_BASE_URL", ""),
                      env("RESEARCH_CHAT_API_KEY") or env("OPENAI_API_KEY", ""))
        self._image = (env("RESEARCH_IMAGE_BASE_URL") or env("OPENAI_BASE_URL", ""),
                       env("RESEARCH_IMAGE_API_KEY") or env("OPENAI_API_KEY", ""))

    def _cached(self, step: str, request: dict, call: Callable[[], tuple[dict, dict]]) -> dict:
        """The cached answer to `request`, made by `call` on a miss; its usage is charged once per run."""
        # The step is part of the key: each round asks for a new painting even when the
        # critic's revision leaves the card unchanged.
        key = hashlib.sha256(json.dumps(dict(request, step=step), sort_keys=True).encode()).hexdigest()
        if key not in self.cache:
            if self.replay:
                raise SystemExit(f"--replay: figure request {key[:12]} ({step}) is not cached")
            answer, usage = call()
            rec = {"key": key, "step": step, **answer, "usage": usage}
            with self.cache_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.cache[key] = rec
        rec = self.cache[key]
        if key in self.charged:
            return rec
        self.charged.add(key)
        self.on_usage({"step": step, "model": request["model"], "total_tokens": rec["usage"].get("total_tokens", 0),
                       "measured": bool(rec["usage"]), "source_record": f"{self.cache_path.name}#{key[:16]}"})
        return rec

    def superseded_tokens(self) -> int:
        """Tokens of cached answers this run did not ask for (earlier requests); they were still paid for."""
        return sum(rec["usage"].get("total_tokens", 0) for key, rec in self.cache.items() if key not in self.charged)

    def chat(self, step: str, system: str, user: str, image: bytes | None = None,
             cache_as: dict | None = None) -> str:
        """The model's answer. `cache_as` replaces the user text in the cache key, for a
        request built from material the run does not keep (a paper's full text)."""
        content: Any = user
        if image is not None:
            content = [{"type": "text", "text": user},
                       {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode()}}]
        body = {"model": self.chat_model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
        request = dict(body, messages=[body["messages"][0], cache_as or {
            "role": "user", "content": user, "image_sha256": hashlib.sha256(image).hexdigest() if image else None}])

        def call() -> tuple[dict, dict]:
            base, key = self._chat
            data = _post(base.rstrip("/") + "/chat/completions", key, body)
            return {"text": data["choices"][0]["message"]["content"] or ""}, data.get("usage") or {}

        return self._cached(step, request, call)["text"]

    def paint(self, step: str, prompt: str) -> bytes:
        body = {"model": self.image_model, "prompt": prompt, "n": 1, "size": self.image_size,
                "response_format": "b64_json"}

        def call() -> tuple[dict, dict]:
            base, key = self._image
            data = _post(base.rstrip("/") + "/images/generations", key, body)
            png = base64.b64decode(data["data"][0]["b64_json"])
            self.image_dir.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(png).hexdigest()[:16] + ".png"
            (self.image_dir / name).write_bytes(png)
            return {"image": f"{self.image_dir.name}/{name}"}, data.get("usage") or {}

        rec = self._cached(step, body, call)
        return (self.cache_path.parent / rec["image"]).read_bytes()
