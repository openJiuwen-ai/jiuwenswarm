"""Co-scribe: the cloud-document co-editing toolset.

Importing this package **does not require** the google extras. The provider
abstraction and its types import unconditionally; only instantiating
``GoogleDocsProvider`` needs ``google-api-python-client``.
"""

from .providers.provider import (
    AgentIdentity,
    DocCapabilities,
    DocComment,
    DocProvider,
    DocRef,
    DocReply,
    DocSnapshot,
    EditResult,
    EditStatus,
    ProviderError,
    ReplyRef,
    Segment,
)

__all__ = [
    "AgentIdentity",
    "DocCapabilities",
    "DocComment",
    "DocProvider",
    "DocRef",
    "DocReply",
    "DocSnapshot",
    "EditResult",
    "EditStatus",
    "ProviderError",
    "ReplyRef",
    "Segment",
]
