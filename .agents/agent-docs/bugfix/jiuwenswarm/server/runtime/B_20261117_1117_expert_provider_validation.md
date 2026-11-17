---
id: B_20261117_1117_expert_provider_validation
title: 第三方专家安装候选项与运行时支持一致
type: B
author: huanghongyun
author_email: huanghongyun1@huawei.com
modules:
  - jiuwenswarm/server/runtime
files:
  - jiuwenswarm/server/runtime/extension_package_manager.py
  - tests/unit_tests/server/extensions/test_extension_package_manager.py
  - tests/unit_tests/agentserver/test_external_harness_helpers.py
designs: []
design_note: 修正当前产品支持的 provider 列表。
---

## 现象与根因

[Issue #6686](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6686) 的上传流程先导入专家压缩包，再安装专家。安装校验使用独立的 provider 白名单，仍接受并推荐 `dsh`。表单和当前产品运行时仅支持 Claude Code 和 Codex。

## 修复与验证

由表单校验、安装校验和会话绑定共用 JiuwenSwarm 的 provider 白名单，仅允许 `claudecode` 和 `codex`。上传流程继续负责安装失败后的回滚，核心的通用 RuntimeSpec 无需变更。

- 开发分支基线 `2cce9d32f`，使用锁定的 agent-core `2f4bedb9c`，macOS / Python 3.11 的定向测试覆盖 ZIP 导入后安装及会话绑定。
- 基线的安装测试会把 `dsh` 列在 Gemini 错误提示里，并允许 DSH 安装。修复后两种包均被拒，提示只列出支持项。
- 通过浏览器上传对话框投放本地测试 ZIP，实际服务对 Gemini 和 DSH 均返回仅列 Claude Code 与 Codex 的错误。
- 支持项已同步到英文和中文 AgentTeam 指南。
