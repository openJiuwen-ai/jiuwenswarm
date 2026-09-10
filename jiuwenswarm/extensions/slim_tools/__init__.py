# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SlimTools extension: tool surface slimming as a loadable extension.

When SLIM_TOOLS_ENABLED=1, runtime-patches the adapter to:
- drop wiki_ingest / wiki_query / wiki_lint tool cards
- drop acp_chat tool card
- retire the metadata-only audio fallback (audio_metadata)
- build SlimSysOperationRail (no powershell / list_files)
- remove list_files from the progressive eager-tools default
- register a merged search_skill (install/uninstall folded in, auto-install)

Zero modification to any stock jiuwenswarm source file.
"""

from jiuwenswarm.extensions.slim_tools.extension import register_extensions

__all__ = ["register_extensions"]
