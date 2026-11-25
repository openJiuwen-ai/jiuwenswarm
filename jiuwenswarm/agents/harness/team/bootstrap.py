# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Bootstrap helpers for JiuwenSwarm team integrations."""

from __future__ import annotations

from jiuwenswarm.common.utils import get_domain_data_dir


def configure_agent_teams_home() -> None:
    """Point openjiuwen.agent_teams at the domain-zone root.

    团队名册/任务/工作区为跨会话业务数据，随 domain 区（共享卷）持久化。
    """
    from openjiuwen.agent_teams.paths import configure_openjiuwen_home

    configure_openjiuwen_home(get_domain_data_dir())
    # Register installer only; Catalog/Launcher stay lazy until first org tool use.
    from jiuwenswarm.agents.harness.team.expert_org.wiring import (
        register_expert_adapter_installer,
    )
    from jiuwenswarm.agents.harness.team.summary_org.wiring import (
        register_summary_adapter_installer,
    )

    register_expert_adapter_installer()
    register_summary_adapter_installer()
    # Team leaders browse and spawn installed experts through this catalog.
    from jiuwenswarm.agents.harness.team.experts import install_expert_catalog

    install_expert_catalog()
