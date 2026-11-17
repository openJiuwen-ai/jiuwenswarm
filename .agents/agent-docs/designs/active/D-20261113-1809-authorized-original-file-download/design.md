---
id: D-20261113-1809-authorized-original-file-download
title: 授权附件改为原文件、不过期令牌与 SHA-256 留痕
slug: authorized-original-file-download
type: D
status: in-development
stage: active
author: nypdmax
author_email: chengkunck@gmail.com
reviewers:
  - independent-reviewer (/root/download_plan_review)
approved-date: 2026-11-14
created: 2026-11-13
updated: 2026-11-17
domain: security
modules:
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/common
  - jiuwenswarm/server/runtime/gateway_adapter
files:
  - jiuwenswarm/agents/harness/common/tools/send_file_to_user.py
  - jiuwenswarm/common/verified_download_assets.py
  - jiuwenswarm/common/file_download_token.py
  - jiuwenswarm/server/runtime/gateway_adapter/workspace_file_adapter.py
tags: [auto-permission, file-delivery, sha256]
scope: 最小取消文件快照与下载 TTL，复用现有登记、token 与历史，仅对四个生产文件作必要适配。
related: []
---

# 授权附件改为原文件、不过期令牌与 SHA-256 留痕

## 目标与范围

非企业模式下，要求精确执行授权的文件交付改为原文件引用，取消固定 600 秒下载限制并保留首次 SHA-256。仅适配既有四个生产文件，保留 token 结构、普通交付和旧快照兼容。

## 关键决策

- 复用 Owner 的 sidecar、stage/commit/revoke、token 和历史，不保存文件字节快照。
- SHA 在消费路径/目标授权后、生成链接前采集，表示交付时点；后续文件变化只影响下载内容，不覆盖历史摘要或自动重审。
- 仅已登记受管引用使用数值 exp=0；签名、会话、登记匹配和原路径授权绑定继续生效。清理登记不能删除原文件。
- Windows 默认登记目录复用用户数据根下的 download-assets，取消 PID 依赖；其他平台保留既有 UID 目录，显式配置仍优先。

不新增审批链改造、审计库、重签或平台读取框架：现有登记与历史已承担这些目标所需的职责。当前契约见 [授权文件下载规约](../../../specs/jiuwenswarm/common/S_01_authorized-file-download.md)。

## 验收与确认

验证无快照、无固定 TTL、历史 SHA 恢复、当前内容/大小下载、拒绝未授权或无登记访问，以及撤销保留原文件和旧快照兼容。

四文件方案于 2026-11-14 经用户确认并独立评审 Approve；Windows 稳定登记目录修订于 2026-11-16 同样确认并评审通过。实施及实际验证由关联 F 记录维护。
