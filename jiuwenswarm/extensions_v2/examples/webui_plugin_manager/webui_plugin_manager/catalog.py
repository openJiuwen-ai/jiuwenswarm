# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic demonstration catalog rendered by the plugin-manager page.

The example deliberately owns a local, read-only catalog instead of querying the
extension manager: plugins cannot reach package management through public ops
yet (see README). Keeping the data in one module makes that seam explicit.
"""

from __future__ import annotations

from dataclasses import dataclass

SOURCE_LOCAL = "local"
SOURCE_BUILTIN = "builtin"
SOURCE_COMMUNITY = "community"

SOURCE_LABELS = {
    SOURCE_LOCAL: "本地 / Local",
    SOURCE_BUILTIN: "内置 / Builtin",
    SOURCE_COMMUNITY: "社区 / Community",
}


@dataclass(frozen=True, slots=True)
class PluginEntry:
    """One plugin row shown by the manager page."""

    plugin_id: str
    name: str
    version: str
    latest_version: str
    author: str
    source: str
    description: str
    icon: str
    tags: tuple[str, ...]
    permission_label: str
    install_path: str
    enabled: bool = True
    builtin: bool = False
    incompatible_reason: str = ""

    @property
    def update_available(self) -> bool:
        """A newer version is publishable for this demo row."""

        return bool(self.latest_version and self.latest_version != self.version)

    @property
    def can_uninstall(self) -> bool:
        """Builtin and incompatible rows stay installed in the demo."""

        return not self.builtin and not self.incompatible_reason


CATALOG: tuple[PluginEntry, ...] = (
    PluginEntry(
        plugin_id="webui.plugin-manager",
        name="插件管理 / Plugin manager",
        version="1.0.0",
        latest_version="",
        author="JiuwenSwarm",
        source=SOURCE_LOCAL,
        description="当前页面所在的顶层插件，位于侧边栏导航区。 / The top-level plugin that owns this page.",
        icon="layout-grid",
        tags=("WebUI", "示例 / Example"),
        permission_label="界面渲染 / UI render",
        install_path="~/.jiuwenswarm/extension_pkgs/webui.plugin-manager/1.0.0",
        builtin=True,
    ),
    PluginEntry(
        plugin_id="webui.workbench-v2",
        name="交互工作台 / Interaction workbench",
        version="1.0.9",
        latest_version="1.1.0",
        author="JiuwenSwarm",
        source=SOURCE_LOCAL,
        description="覆盖全部会话槽位与右侧工作区的综合示例。 / Covers every conversation slot and the right workspace.",
        icon="workflow",
        tags=("WebUI", "槽位 / Slots"),
        permission_label="会话读取 / Session read",
        install_path="~/.jiuwenswarm/extension_pkgs/webui.workbench-v2/1.0.9",
    ),
    PluginEntry(
        plugin_id="webui.token-weather",
        name="Token 天气 / Token weather",
        version="1.0.0",
        latest_version="",
        author="JiuwenSwarm",
        source=SOURCE_LOCAL,
        description="在消息 meta 与输入区下方展示本轮用量和上下文摘要。 / Shows turn usage and the context summary.",
        icon="sparkles",
        tags=("WebUI", "用量 / Usage"),
        permission_label="用量查询 / Usage read",
        install_path="~/.jiuwenswarm/extension_pkgs/webui.token-weather/1.0.0",
    ),
    PluginEntry(
        plugin_id="core.session-inspector",
        name="会话检查器 / Session inspector",
        version="2.3.1",
        latest_version="",
        author="JiuwenSwarm Core",
        source=SOURCE_BUILTIN,
        description="只读展示会话模型、仓库与历史事实。 / Read-only view of session model, repository and history facts.",
        icon="messages-square",
        tags=("Core", "只读 / Read only"),
        permission_label="会话查询 / Session query",
        install_path="<builtin>/core.session-inspector",
        builtin=True,
    ),
    PluginEntry(
        plugin_id="core.model-router",
        name="模型路由器 / Model router",
        version="3.0.4",
        latest_version="3.1.0",
        author="JiuwenSwarm Core",
        source=SOURCE_BUILTIN,
        description="按任务类型选择模型并提供回退链。 / Picks a model per task and keeps a fallback chain.",
        icon="git-branch",
        tags=("Core", "模型 / Model"),
        permission_label="模型配置 / Model config",
        install_path="<builtin>/core.model-router",
        builtin=True,
    ),
    PluginEntry(
        plugin_id="community.markdown-exporter",
        name="Markdown 导出 / Markdown exporter",
        version="0.8.2",
        latest_version="",
        author="Community",
        source=SOURCE_COMMUNITY,
        description="把单轮回复导出为 Markdown 文件。 / Exports one assistant turn as a Markdown file.",
        icon="file-text",
        tags=("导出 / Export",),
        permission_label="文件写入 / File write",
        install_path="~/.jiuwenswarm/extension_pkgs/community.markdown-exporter/0.8.2",
        enabled=False,
    ),
    PluginEntry(
        plugin_id="community.theme-pack",
        name="主题包 / Theme pack",
        version="1.4.0",
        latest_version="1.5.0",
        author="Community",
        source=SOURCE_COMMUNITY,
        description="提供额外的浅色与深色界面主题。 / Ships extra light and dark interface themes.",
        icon="image",
        tags=("主题 / Theme",),
        permission_label="界面渲染 / UI render",
        install_path="~/.jiuwenswarm/extension_pkgs/community.theme-pack/1.4.0",
    ),
    PluginEntry(
        plugin_id="community.usage-analytics",
        name="用量分析 / Usage analytics",
        version="0.3.0",
        latest_version="",
        author="Community",
        source=SOURCE_COMMUNITY,
        description="按会话统计 token 消耗并导出报表。 / Aggregates token usage per session and exports a report.",
        icon="chart-column",
        tags=("统计 / Analytics",),
        permission_label="用量查询 / Usage read",
        install_path="~/.jiuwenswarm/extension_pkgs/community.usage-analytics/0.3.0",
        enabled=False,
        incompatible_reason="要求宿主 1.2+ / Requires host 1.2+",
    ),
    PluginEntry(
        plugin_id="local.hello-world",
        name="示例问候 / Hello world",
        version="1.0.0",
        latest_version="",
        author="Workspace",
        source=SOURCE_LOCAL,
        description="最小插件骨架，用于验证安装与启停流程。 / Minimal skeleton used to verify install and enable flows.",
        icon="bot",
        tags=("示例 / Example",),
        permission_label="无 / None",
        install_path="~/.jiuwenswarm/extension_pkgs/local.hello-world/1.0.0",
        enabled=False,
    ),
)

CATALOG_BY_ID = {entry.plugin_id: entry for entry in CATALOG}
