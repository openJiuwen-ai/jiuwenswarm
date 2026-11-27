# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""
Ollama Embedding Model Implementation

Implementation of Ollama embedding model.
"""

import asyncio
import os
from itertools import chain
from typing import List, Optional

import requests


def _ssl_verify() -> bool:
    return os.environ.get("JIUWENCLAW_SSL_VERIFY", "true").strip().lower() not in ("0", "false", "no", "off")


from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.logging import logger
from openjiuwen.core.retrieval.common.callbacks import BaseCallback
from openjiuwen.core.retrieval.common.config import EmbeddingConfig
from openjiuwen.core.retrieval.embedding.base import Embedding


class OllamaEmbedding(Embedding):
    """Ollama embedding model implementation."""

    def __init__(
        self,
        config: EmbeddingConfig,
        hf_tokenizer_name: Optional[str] = None,
        timeout: int = 60,
        max_retries: int = 3,
        extra_headers: Optional[dict] = None,
        max_batch_size: int = 8,
        max_concurrent: int = 50,
        dimension: Optional[int] = None,
    ):
        """
        Initialize Ollama embedder.

        Args:
            config: Embedding model configuration
            hf_tokenizer_name: HuggingFace tokenizer name (optional)
            timeout: Request timeout in seconds
            max_retries: Maximum retry count
            extra_headers: Additional request headers
        """
        self.config = config
        self.model_name = config.model_name
        self.base_url = (config.base_url or "").rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.embed_url = f"{self.base_url}/api/embed"
        self._headers = extra_headers or {}
        self.max_batch_size = max_batch_size
        self.limiter = asyncio.Semaphore(max_concurrent)
        self._dimension: Optional[int] = None
        if dimension is not None:
            self._dimension = dimension

        # Initialize tokenizer if provided
        if hf_tokenizer_name:
            try:
                from transformers import AutoTokenizer

                self._tokenizer = AutoTokenizer.from_pretrained(hf_tokenizer_name)
            except ImportError:
                logger.warning("transformers not available, tokenizer disabled")
                self._tokenizer = None
        else:
            self._tokenizer = None

        # Test connection and model availability
        self._verify_model_availability()

    @property
    def tokenizer(self):
        return self._tokenizer

    def _verify_model_availability(self):
        """Verify that Ollama is running and the model is available."""
        try:
            # Check if Ollama is running
            response = requests.get(f"{self.base_url}/api/tags", timeout=5, verify=_ssl_verify())
            response.raise_for_status()

            # Check if the model is available
            models = response.json().get("models", [])
            model_names = [model["name"] for model in models]

            if self.model_name not in model_names:
                raise build_error(
                    StatusCode.RETRIEVAL_EMBEDDING_MODEL_NOT_FOUND,
                    error_msg=f"Model '{self.model_name}' not found in available models: {model_names}. "
                    f"Make sure to pull the model first: ollama pull {self.model_name}",
                )
        except requests.exceptions.RequestException as e:
            raise build_error(
                StatusCode.RETRIEVAL_EMBEDDING_CALL_FAILED,
                error_msg=f"Could not connect to Ollama at {self.base_url}. Is Ollama running?",
                cause=e,
            ) from e

    @property
    def dimension(self) -> int:
        """Return embedding dimension"""
        if self._dimension is None:
            # Get dimension by embedding a test text
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If event loop is running, use default value
                    self._dimension = 768
                else:
                    test_embedding = loop.run_until_complete(self._get_ollama_embedding("X"))
                    if test_embedding:
                        self._dimension = len(test_embedding[0])
                    else:
                        self._dimension = 768
            except Exception:
                # If failed to get, use default value
                self._dimension = 768
        return self._dimension

    async def embed_query(self, text: str, **kwargs) -> List[float]:
        if not text.strip():
            raise build_error(
                StatusCode.RETRIEVAL_EMBEDDING_INPUT_INVALID, error_msg="Empty text provided for embedding"
            )

        embeddings = await self._get_embeddings(text, **kwargs)
        return embeddings[0]

    async def embed_documents(
        self,
        texts: List[str],
        batch_size: Optional[int] = None,
        **kwargs,
    ) -> List[List[float]]:
        if not texts:
            raise build_error(StatusCode.RETRIEVAL_EMBEDDING_INPUT_INVALID, error_msg="Empty texts list provided")
        callback_cls = kwargs.pop("callback_cls", BaseCallback)
        if not isinstance(callback_cls, type) or not issubclass(callback_cls, BaseCallback):
            raise build_error(
                StatusCode.RETRIEVAL_EMBEDDING_CALLBACK_INVALID,
                error_msg=(
                    f"callback_cls in OllamaEmbedding.embed_documents must be a subclass of "
                    f"BaseCallback, got {type(callback_cls)}"
                ),
            )

        # Filter out empty texts
        non_empty_texts = [text for text in texts if text.strip()]
        if len(non_empty_texts) != len(texts):
            raise build_error(
                StatusCode.RETRIEVAL_EMBEDDING_INPUT_INVALID,
                error_msg=f"{len(texts) - len(non_empty_texts)} chunks are empty while embedding",
            )

        if not non_empty_texts:
            raise build_error(
                StatusCode.RETRIEVAL_EMBEDDING_INPUT_INVALID, error_msg="All texts are empty after filtering"
            )

        # Respect caller batch_size but never exceed configured max_batch_size
        bsz = batch_size or self.max_batch_size or 1
        if self.max_batch_size:
            bsz = min(bsz, self.max_batch_size)

        indices = list(range(0, len(non_empty_texts), bsz))
        callback_obj = callback_cls(seq=indices)

        async def process_batch(i: int) -> List[List[float]]:
            """Process a single batch with semaphore for concurrency control."""
            async with self.limiter:
                j = i + bsz
                batch = non_empty_texts[i:j]
                embeddings = await self._get_embeddings(batch, **kwargs)
                callback_obj(start_idx=i, end_idx=j, batch=batch)
                return embeddings

        # Create and run tasks for all batches concurrently
        tasks = [process_batch(i) for i in indices]
        results = await asyncio.gather(*tasks)
        all_embeddings = list(chain.from_iterable(results))

        return all_embeddings

    async def _get_embeddings(self, text: str | List[str], **kwargs) -> List[List[float]]:
        """Get embedding vectors"""

        payload = {
            "model": self.model_name,
            "input": text,
            "truncate": False,
            **kwargs,
        }

        for attempt in range(self.max_retries):
            try:
                response = await asyncio.to_thread(
                    requests.post,
                    self.embed_url,
                    json=payload,
                    headers=self._headers,
                    timeout=self.timeout,
                    verify=_ssl_verify(),
                )
                response.raise_for_status()
                result = response.json()
                if "embeddings" not in result:
                    raise build_error(
                        StatusCode.RETRIEVAL_EMBEDDING_RESPONSE_INVALID,
                        error_msg=f"No embeddings in response: {result}",
                    )

                return result["embeddings"]

            except requests.exceptions.RequestException as e:
                if attempt == self.max_retries - 1:
                    raise build_error(
                        StatusCode.RETRIEVAL_EMBEDDING_REQUEST_CALL_FAILED,
                        error_msg=f"Failed to get embedding after {self.max_retries} attempts",
                        cause=e,
                    ) from e
                logger.warning(f"Attempt {attempt + 1} failed, retrying: {e}")

        raise build_error(
            StatusCode.RETRIEVAL_EMBEDDING_UNREACHABLE_CALL_FAILED, error_msg="This should never be reached"
        )

    def _get_embeddings_sync(self, text: str | List[str], **kwargs) -> List[List[float]]:
        """Get embedding vectors"""

        payload = {
            "model": self.model_name,
            "input": text,
            "truncate": False,
            **kwargs,
        }

        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    self.embed_url,
                    json=payload,
                    headers=self._headers,
                    timeout=self.timeout,
                    verify=_ssl_verify(),
                )
                response.raise_for_status()
                result = response.json()
                if "embeddings" not in result:
                    raise build_error(
                        StatusCode.RETRIEVAL_EMBEDDING_RESPONSE_INVALID,
                        error_msg=f"No embeddings in response: {result}",
                    )

                return result["embeddings"]

            except requests.exceptions.RequestException as e:
                if attempt == self.max_retries - 1:
                    raise build_error(
                        StatusCode.RETRIEVAL_EMBEDDING_REQUEST_CALL_FAILED,
                        error_msg=f"Failed to get embedding after {self.max_retries} attempts",
                        cause=e,
                    ) from e
                logger.warning(f"Attempt {attempt + 1} failed, retrying: {e}")

        raise build_error(
            StatusCode.RETRIEVAL_EMBEDDING_UNREACHABLE_CALL_FAILED, error_msg="This should never be reached"
        )
