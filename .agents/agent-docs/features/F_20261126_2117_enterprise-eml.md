---
id: F_20261126_2117_enterprise-eml
title: Web 会话上传和读取 EML 邮件
type: F
author: linda
author_email: weipingtao@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/server/runtime/attachments
files:
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/InputArea.tsx
  - jiuwenswarm/agents/harness/common/tools/email_tools.py
  - jiuwenswarm/agents/harness/common/tools/session_messaging_toolkit.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/attachments/upload_storage.py
designs: []
design_note: 在需求会话中确认企业版和个人版共用实现，无独立设计文档。
---

# Web 会话上传和读取 EML 邮件

共用的 Web 聊天文件选择器加入 `.eml`，继续使用现有文档上传协议。Agent 按需调用只读工具 `read_email(file_path)`，根据运行时绑定的会话读取上传原件。企业版读取仍要求租户绑定，个人版从当前工作空间读取。工具返回邮件头、正文和附件名称，以纯文本优先，纯 HTML 正文提取为文本。单个 EML 原件可读取至 20 MiB，超过上限即返回错误。正文和附件清单超出输出上限时有截断标记。

解析放在工具调用阶段，使上传继续按文件处理。标准库处理 MIME 和字符编码，复用现有会话上传文件的读取边界与渐进式工具发现。Agent 读取需要 Linux 或 macOS 服务端。

测试覆盖两版输入框选择和文档持久化、模拟 OBS 导入、中文 HTML 与编码、纯文本与引文、附件清单、读取限制、跨会话和跨租户拒绝，以及原有文档工具与附件传输。HTML 正文回归用例验证 `<br/>` 显示为一次换行，并省略未闭合 `<script>` 后的内容。

- Python 分组验证通过 385 项，包含邮件与附件链路 157 项、AgentServer 回归 61 项、会话消息回归 167 项。
- 前端输入框 113 项通过，2 项失败均在原始 `develop` 复现。此前已验证聊天附件和权限应答传输套件。

真实企业部署、OBS 服务和模型问答需要在部署环境验收。后续可独立支持邮件内附件内容和 Outlook `.msg`。
