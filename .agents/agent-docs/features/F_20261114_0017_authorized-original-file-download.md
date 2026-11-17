---
id: F_20261114_0017_authorized-original-file-download
title: 授权附件原文件引用与 SHA-256 留痕实施
type: F
author: nypdmax
author_email: chengkunck@gmail.com
created: 2026-11-14
updated: 2026-11-17
modules:
  - jiuwenswarm/common
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/server/runtime/gateway_adapter
files:
  - jiuwenswarm/common/verified_download_assets.py
  - jiuwenswarm/common/file_download_token.py
  - jiuwenswarm/agents/harness/common/tools/send_file_to_user.py
  - jiuwenswarm/server/runtime/gateway_adapter/workspace_file_adapter.py
designs:
  - D-20261113-1809-authorized-original-file-download
---

# 授权附件原文件引用与 SHA-256 留痕实施

## 实施与取舍

落实 [四文件方案](../designs/active/D-20261113-1809-authorized-original-file-download/design.md)，复用既有登记、摘要读取、token 和历史，保留 0.2.8 的宿主服务与仓内 Gateway 分支；当前行为见 [下载规约](../specs/jiuwenswarm/common/S_01_authorized-file-download.md)。不扩展审批链或另建审计/重签机制，SHA 仅代表交付阶段读取到的指纹。

登记与校验统一使用规范路径，源读取保留原输入的 no-follow 检查，投递前核对宿主已选定路径，解决父目录别名失配并拒绝授权后重定向。Windows 默认目录移除 PID，复用用户数据根；本分支无对应 ASR 配置逻辑，未迁移该项。

## 验证摘要

- 2026-11-16，macOS arm64 / Python 3.11.4，0.2.8 原锁冻结环境：11 个下载测试文件，238 passed、1 skipped；跳过原因为缺少可选 webview。
- 覆盖引用登记、旧快照、签名/会话/登记拒绝、当前内容与大小、Range/分块、历史 SHA 恢复、撤销及原文件保留。
- 此前候选在原生 Windows / Python 3.11.16 完成登记、进程退出后恢复、变长、变短、清空、缺失、撤销七个独立进程阶段；该证据限定此前实测依赖组合，未覆盖 0.2.8 锁定组合。
- 代码实施及分支迁移的独立复核为 Approve，无范围内阻塞项。

## 限制

定向结果不代表完整 CI 或浏览器/真实模型 E2E 通过。原文件可变可删、登记可能丢失及不过期元数据保留等边界以规约为准。

关联 [Issue #6687](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6687)。
