"""Download limits shared with the verified-file chunk protocol."""

from __future__ import annotations

from typing import Final

# Matches AgentServer ``workspace_file_adapter._VERIFIED_DOWNLOAD_CHUNK_MAX_BYTES``.
_VERIFIED_DOWNLOAD_CHUNK_MAX_BYTES: Final[int] = 512 * 1024
