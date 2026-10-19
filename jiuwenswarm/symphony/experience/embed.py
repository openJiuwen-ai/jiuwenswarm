from __future__ import annotations

import logging
import time
from typing import Any

LOGGER = logging.getLogger(__name__)


class EmbeddingClient:
    """Minimal embedding client wrapping any OpenAI-compatible embeddings endpoint.

    Supports two backends:
      1. OpenAI-compatible API (base_url + api_key) — calls /v1/embeddings
      2. Local sentence-transformers model (model_name) — runs locally

    Pass exactly one of (base_url, api_key) or model_name.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str = "",
        model: str = "",
        model_name: str = "",
        normalize: bool = True,
        dimension: int | None = None,
        api_batch_size: int | None = None,
        api_timeout: float = 60.0,
    ) -> None:
        self._backend: str = ""
        self._normalize = bool(normalize)
        self._dimension = dimension
        self._total_tokens: int = 0
        # Max texts per API call. DashScope text-embedding-v3 rejects lists longer
        # than 10 (HTTP 400 "batch size ... should not be larger than 10"); a
        # self-hosted OpenAI-compatible server (e.g. local bge-m3) typically has
        # no such cap, so callers using those should pass api_batch_size=64+.
        self._api_batch_size = 10 if api_batch_size is None else int(api_batch_size)
        self._api_max_retries = 5
        self._api_timeout = float(api_timeout)

        # Path 1: OpenAI-compatible API
        if base_url:
            try:
                from openai import OpenAI
                self._api_client = OpenAI(
                    base_url=base_url, api_key=api_key or "dummy", timeout=self._api_timeout,
                )
                self._api_model = str(model or "text-embedding-3-small")
                self._backend = "openai_api"
                LOGGER.info("EmbeddingClient: using OpenAI-compatible API, model=%s", self._api_model)
                return
            except ImportError:
                pass

        # Path 2: Local sentence-transformers
        if model_name:
            try:
                from sentence_transformers import SentenceTransformer
                self._st_model = SentenceTransformer(model_name)
                self._backend = "sentence_transformers"
                LOGGER.info("EmbeddingClient: using local sentence-transformers, model=%s", model_name)
                return
            except ImportError:
                pass

        raise RuntimeError(
            "EmbeddingClient: could not initialize any embedding backend.\n"
            "Option 1: install 'openai' and provide base_url.\n"
            "Option 2: install 'sentence-transformers' and provide model_name."
        )

    @property
    def dimension(self) -> int | None:
        """Return the configured embedding vector dimension, or None if unset."""
        return self._dimension

    def reset_token_counter(self) -> int:
        """Reset and return the accumulated token counter."""
        prev = self._total_tokens
        self._total_tokens = 0
        return prev

    def embed(self, text: str) -> list[float]:
        """Return a normalized embedding vector for the given text."""
        if self._backend == "openai_api":
            return self._embed_api(text)
        if self._backend == "sentence_transformers":
            return self._embed_local(text)
        raise RuntimeError("No embedding backend initialized")

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings for multiple texts."""
        if self._backend == "openai_api":
            return self._embed_api_batch(texts)
        if self._backend == "sentence_transformers":
            vecs = self._st_model.encode(texts, normalize_embeddings=self._normalize, show_progress_bar=False)
            return vecs.tolist()
        raise RuntimeError("No embedding backend initialized")

    # -- private --

    def _embed_api(self, text: str) -> list[float]:
        return self._embed_api_batch([text])[0]

    def _embed_api_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch API embedding with retry/backoff.

        Splits into sub-batches (``_api_batch_size`` each), one API call per
        sub-batch. DashScope/OpenAI accept a list under ``input`` and return
        one embedding per element (indexed by ``data[i].index``). Retries on
        transient errors (429 / 5xx / connection) with exponential backoff.
        """
        results: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
        batch = self._api_batch_size
        for start in range(0, len(texts), batch):
            chunk = texts[start:start + batch]
            vecs = self._embed_api_call(chunk)
            for i, v in enumerate(vecs):
                results[start + i] = v
        # Should be fully filled by now; guard against any None just in case.
        for i, r in enumerate(results):
            if r is None:
                results[i] = self._embed_api_call([texts[i]])[0]
        return results

    def _embed_api_call(self, texts: list[str]) -> list[list[float]]:
        from openai import (
            APITimeoutError,
            APIStatusError,
            APIConnectionError,
            BadRequestError,
            RateLimitError,
        )
        params: dict[str, Any] = {"input": texts, "model": self._api_model}
        if self._dimension is not None:
            params["dimensions"] = self._dimension
        last_exc: Exception | None = None
        for attempt in range(self._api_max_retries):
            try:
                resp = self._api_client.embeddings.create(**params)
                # Preserve input order via the data[].index field
                by_index: dict[int, list[float]] = {}
                for d in resp.data:
                    by_index[int(getattr(d, "index", len(by_index)))] = list(d.embedding)
                vecs = [by_index[i] for i in range(len(texts))]
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    self._total_tokens += getattr(usage, "total_tokens", 0) or 0
                if self._normalize:
                    vecs = [_l2_normalize(v) for v in vecs]
                return vecs
            except BadRequestError as exc:
                # 400 — caller error (e.g. too many texts, bad params).
                # Retrying won't help; surface immediately.
                raise RuntimeError(
                    f"EmbeddingClient: embeddings.create bad request (batch={len(texts)}): {exc}"
                ) from exc
            except RateLimitError as exc:
                last_exc = exc
                wait = min(2 ** attempt, 30)
                LOGGER.warning(
                    "EmbeddingClient: rate limited on batch of %d (attempt %d), retrying in %ds",
                    len(texts), attempt + 1, wait,
                )
                time.sleep(wait)
            except (APITimeoutError, APIConnectionError, OSError) as exc:
                # Transient transport/timeout errors — retry with backoff.
                last_exc = exc
                wait = min(2 ** attempt, 30)
                LOGGER.warning(
                    "EmbeddingClient: transient error on batch of %d (attempt %d): %s, retrying in %ds",
                    len(texts), attempt + 1, type(exc).__name__, wait,
                )
                time.sleep(wait)
            except APIStatusError as exc:
                # HTTP status error. 4xx (except rate-limit caught above) is a
                # caller error — surface immediately. 5xx retries.
                status = getattr(exc, "status_code", None)
                if status is not None and 400 <= status < 500:
                    raise RuntimeError(
                        f"EmbeddingClient: embeddings.create client error {status} "
                        f"(batch={len(texts)}): {exc}"
                    ) from exc
                last_exc = exc
                wait = min(2 ** attempt, 30)
                LOGGER.warning(
                    "EmbeddingClient: API error on batch of %d (attempt %d): %s, retrying in %ds",
                    len(texts), attempt + 1, exc, wait,
                )
                time.sleep(wait)
        raise RuntimeError(
            f"EmbeddingClient: embeddings.create failed after {self._api_max_retries} retries: {last_exc}"
        )

    def _embed_local(self, text: str) -> list[float]:
        vec = self._st_model.encode(text, normalize_embeddings=self._normalize).tolist()
        return vec


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0:
        return vec
    return [x / norm for x in vec]


__all__ = ["EmbeddingClient"]
