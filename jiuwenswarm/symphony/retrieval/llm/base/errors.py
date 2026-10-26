from __future__ import annotations


class LLMClientError(RuntimeError):
    """Base error for progressive LLM client failures."""


class UnsupportedCapability(LLMClientError):
    """Raised when a client is asked to use a capability it does not expose."""


class LLMRequestError(LLMClientError):
    """Raised when a backend request fails."""


class CandidateEncodingError(LLMClientError):
    """Raised when visible candidate codes cannot be encoded safely."""


class CandidateScoringError(LLMClientError):
    """Raised when candidate scoring fails."""


class PrefixCacheError(LLMClientError):
    """Raised when prefix-cache generation cannot use the low-latency path."""


class PrefixCacheUnavailable(PrefixCacheError):
    """Raised when a required prefix-cache handle or slot is unavailable."""


class QueryTooLongForPrefixCache(PrefixCacheError):
    """Raised when the dynamic suffix cannot fit in a preallocated cache slot."""


class MaxNewTokensTooLarge(PrefixCacheError):
    """Raised when requested decode length exceeds the preallocated tail budget."""


class PrefixCacheRuntimeOOM(PrefixCacheError):
    """Raised after an OOM while using a prefix-cache slot."""


__all__ = [
    "CandidateEncodingError",
    "CandidateScoringError",
    "LLMClientError",
    "LLMRequestError",
    "MaxNewTokensTooLarge",
    "PrefixCacheError",
    "PrefixCacheRuntimeOOM",
    "PrefixCacheUnavailable",
    "QueryTooLongForPrefixCache",
    "UnsupportedCapability",
]
