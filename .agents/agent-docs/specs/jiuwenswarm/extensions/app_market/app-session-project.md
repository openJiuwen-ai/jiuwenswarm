---
id: S_01_app-session-project
title: 应用角色会话的项目归属
type: S
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/extensions/app_market
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin
files:
  - jiuwenswarm/extensions/app_market/app_market/market_service.py
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md
---

# 应用角色会话的项目归属

应用市场在启动与停止脚本的环境中设置 `WORKSWARM_SOURCE_SESSION`，值为应用清单的 `source_session`；未登记来源时为空，不继承宿主进程中其他应用的来源值。

应用服务端使用既有 Gateway `session.get_metadata` 查询来源会话的 `project_id` 与 `work_mode`，在 `session.create` 中传入这两个字段。消息请求沿用相同工作模式。项目路径由既有创建接口解析，不以应用所在子目录推断项目。工作模式必须与项目匹配，不能固定为 `work`。

应用缺少来源或来源不可访问时，明确报告错误，不静默退回默认项目。来源本身属于默认项目时，继承其默认项目 ID 与工作模式。旧宿主未注入变量时，应用可使用自己保存的来源会话配置。

市场只提供运行上下文，不承担 AI 转发、角色记忆或会话创建；具体应用负责消费上下文。既有应用需要更新自己的客户端才能遵循此约定。

纯对话角色可使用独立的 `execution_profile: app_role`，不改变以上项目与 work_mode 约束。创建时同时传入非空 `role_instructions`（最长 12000 字符），服务端持久化两者并固定到该 session；缺省 profile 的既有会话保持普通执行行为。轻量创建响应额外携带 `execution_profile: app_role`，普通创建响应保持原字段集合。

模型输入包含精简基础系统提示、应用角色规则、该 session 最近完整对话和当前用户文本。基础系统规则定义角色助手身份、输入数据边界、能力边界与格式遵循，不为空。轻量执行不构建通用 DeepAgent，因此不加载项目指令、项目/个人记忆、技能、MCP、插件、工具或子代理。后续聊天不得变更 profile 和人格或装备工具。

轻量角色不进入普通预热池；创建 token 的幂等身份包含 profile 与角色规则。分叉复制 profile、规则和 work_mode；恢复/热更新不能变回普通执行。永续记忆、cron、team 与显式增强执行不受支持；仍保存普通聊天历史，支持流式响应和取消，暂停/继续返回明确错误。角色 adapter 限制模型输入总字符数为 48000，优先保留最近完整对话对，固定系统规则与当前输入超过限制时明确报错，不自行摘要或检索其他记忆。
