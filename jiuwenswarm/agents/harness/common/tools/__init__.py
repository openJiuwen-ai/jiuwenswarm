# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tools for JiuWenSwarm AgentServer."""

from .send_file_to_user import (
    SendFileToolkit,
)
from .send_html_card import (
    SendHtmlCardToolkit,
)
from .xiaoyi_append_reference import (
    XiaoyiAppendReferenceToolkit,
)
from .skill_toolkits import (
    SkillToolkit,
)
from .skill_retrieval_toolkits import (
    is_skill_retrieval_enabled,
    SkillRetrievalToolkit,
)
from .symphony_toolkits import (
    SymphonyToolkit,
)

# Re-export deep openjiuwen symbols at ≤3-layer depth so task_tools.py can comply
# with the G.IMP import-depth lint rule without creating additional files.
try:
    from openjiuwen.core.foundation.tool.tool import tool
except ImportError:
    tool = None  # type: ignore[assignment]

try:
    from openjiuwen.extensions.context_evolver.core import config as ce_config
    from openjiuwen.extensions.context_evolver.core.file_connector.json_file_connector import (
        JSONFileConnector,
    )
    from openjiuwen.extensions.context_evolver.service.task_memory_service import (
        AddMemoryRequest,
        TaskMemoryService,
    )
except ImportError:
    ce_config = None  # type: ignore[assignment]
    JSONFileConnector = None  # type: ignore[assignment]
    TaskMemoryService = None  # type: ignore[assignment]
    AddMemoryRequest = None  # type: ignore[assignment]

__all__ = [
    "SendFileToolkit",
    "SendHtmlCardToolkit",
    "XiaoyiAppendReferenceToolkit",
    "SkillToolkit",
    "is_skill_retrieval_enabled",
    "SkillRetrievalToolkit",
    "SymphonyToolkit",
    # openjiuwen re-exports
    "tool",
    "ce_config",
    "JSONFileConnector",
    "TaskMemoryService",
    "AddMemoryRequest",
]
