# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Memory system type definitions."""

from typing import List, Dict, Any, Optional, Literal
from dataclasses import dataclass


MemorySource = Literal["memory", "sessions"]


@dataclass
class MemorySearchResult:
    """Result from memory search."""
    id: str
    path: str
    source: str
    startLine: int
    endLine: int
    snippet: str
    score: float
    citation: Optional[str] = None


@dataclass
class MemoryProviderStatus:
    """Status of embedding provider."""
    available: bool
    provider: Optional[str] = None
    model: Optional[str] = None
    error: Optional[str] = None


@dataclass
class MemorySyncProgressUpdate:
    """Progress update during memory sync."""
    phase: str
    current: int
    total: int
    message: Optional[str] = None


@dataclass
class FileEntry:
    """Entry representing a file (memory or session)."""
    path: str
    absPath: str
    hash: str
    mtimeMs: int
    size: int


MemoryFileEntry = FileEntry
SessionFileEntry = FileEntry


@dataclass
class MemoryChunk:
    """A chunk of memory content."""
    text: str
    startLine: int
    endLine: int


@dataclass
class FtsStatus:
    """Status of FTS5 full-text search."""
    enabled: bool
    available: bool = False
    error: Optional[str] = None


@dataclass
class VectorStatus:
    """Status of vector search."""
    enabled: bool
    available: bool = False
    error: Optional[str] = None
    dims: Optional[int] = None
