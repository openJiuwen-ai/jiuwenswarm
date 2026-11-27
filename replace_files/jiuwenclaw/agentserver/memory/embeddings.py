# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Embedding providers for memory system."""

import logging
import os
from abc import ABC, abstractmethod
from typing import List, Optional


logger = logging.getLogger(__name__)

class EmbeddingProvider(ABC):
    """Base class for embedding providers."""
    
    id: str = "base"
    model: str = "base"
    dims: int = 0
    
    @abstractmethod
    async def embed_query(self, text: str) -> List[float]:
        """Generate embedding for a query."""
        pass
    
    @abstractmethod
    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple documents."""
        pass


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embedding provider (supports DashScope, OpenAI, etc.)."""
    
    id: str = "openai_compatible"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None
    ):
        from .config import get_embed_config
        
        embed_config = get_embed_config()
        
        self.api_key = api_key or os.getenv("EMBED_KEY") or os.getenv("EMBED_API_KEY") or embed_config.get("api_key", "")
        self.model = model or os.getenv("EMBED_MODEL") or os.getenv("EMBEDDING_MODEL") or embed_config.get("model", "")
        self.base_url = base_url or os.getenv("EMBED_BASE") or os.getenv("EMBED_BASE_URL") or embed_config.get("base_url", "")
        
        if self.base_url and self.base_url.endswith("/embeddings"):
            self.base_url = self.base_url.rsplit("/embeddings", 1)[0]
        
        self.dims = 1024
        self._client = None
    
    def _get_client(self):
        """Get or create HTTP client."""
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client
    
    async def embed_query(self, text: str) -> List[float]:
        """Generate embedding for a query."""
        embeddings = await self.embed_documents([text])
        return embeddings[0] if embeddings else []
    
    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple documents."""
        if not self.api_key:
            raise ValueError("Embedding API key not configured. Set embed.api_key in config.yaml or EMBED_API_KEY environment variable.")
        
        client = self._get_client()
        
        response = await client.post(
            f"{self.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": self.model,
                "input": texts,
                "encoding_format": "float"
            }
        )
        
        if response.status_code != 200:
            raise RuntimeError(f"Embedding API failed: {response.text}")
        
        data = response.json()
        embeddings = []
        for item in sorted(data.get("data", []), key=lambda x: x.get("index", 0)):
            embedding = item.get("embedding", [])
            embeddings.append(embedding)
        
        if embeddings:
            self.dims = len(embeddings[0])
        
        return embeddings


class MockEmbeddingProvider(EmbeddingProvider):
    """Mock embedding provider for testing."""
    
    id: str = "mock"
    model: str = "mock"
    dims: int = 128
    
    async def embed_query(self, text: str) -> List[float]:
        """Generate mock embedding."""
        import hashlib
        import random
        h = hashlib.md5(text.encode()).hexdigest()
        random.seed(h)
        return [random.uniform(-1, 1) for _ in range(128)]
    
    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Generate mock embeddings."""
        return [await self.embed_query(t) for t in texts]


async def create_embedding_provider(
    provider: str = "auto",
    model: Optional[str] = None,
    fallback: str = "mock"
) -> EmbeddingProvider:
    """Create embedding provider based on configuration.
    
    Uses config from config.yaml embed section if environment variables not set.
    
    Args:
        provider: Provider name (openai_compatible, auto, mock)
        model: Model name
        fallback: Fallback provider if auto-detection fails
    
    Returns:
        Embedding provider instance
    """
    from .config import get_embed_config
    
    if provider == "mock":
        return MockEmbeddingProvider()
    
    embed_config = get_embed_config()
    embed_key = os.getenv("EMBED_KEY") or os.getenv("EMBED_API_KEY") or embed_config.get("api_key", "")
    
    if embed_key:
        return OpenAICompatibleEmbeddingProvider(
            api_key=embed_key,
            model=model or os.getenv("EMBED_MODEL") or os.getenv("EMBEDDING_MODEL") or embed_config.get("model"),
            base_url=os.getenv("EMBED_BASE") or os.getenv("EMBED_BASE_URL") or embed_config.get("base_url")
        )
    
    if fallback == "mock":
        logger.warning("Embedding API key not found, using mock provider")
        return MockEmbeddingProvider()
    
    raise ValueError(
        "Embedding API key not configured. Set embed.api_key in config.yaml or EMBED_API_KEY environment variable."
    )
