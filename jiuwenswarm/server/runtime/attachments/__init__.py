"""附件工具中立包（Phase 2 WorkspaceFileAdapter 复用）。

原位于 ``gateway/`` 的浏览器上传附件工具在 Phase 2 迁移到 AgentServer 侧，
由适配器（``server/runtime/gateway_adapter``）直接复用。按方案约束，适配器
不得反向依赖 ``gateway.*``，故统一落在 ``server/runtime`` 中立层：

- ``upload_storage``：上传文件名/session 目录安全化规则；
- ``media_attachments``：浏览器上传图片的 base64 解码与落盘；
- ``document_attachments``：文档本地路径黑名单校验（不落盘）。
"""
