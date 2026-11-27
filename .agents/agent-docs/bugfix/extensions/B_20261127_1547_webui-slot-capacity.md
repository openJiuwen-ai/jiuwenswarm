---
id: B_20261127_1547_webui-slot-capacity
title: WebUI 槽位容量与投递回滚修复
type: B
author: kusanagima
author_email: kusanagima@foxmail.com
modules:
  - jiuwenswarm/extensions_v2/webui
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/extensions_v2/webui/constants.py
  - jiuwenswarm/extensions_v2/webui/registry.py
  - jiuwenswarm/extensions_v2/webui/core_handlers/render_core.py
  - jiuwenswarm/channels/web/frontend/src/features/webui/catalog/slots.ts
  - jiuwenswarm/channels/web/frontend/src/components/SessionSidebar/SessionSidebar.css
  - tests/unit_tests/extensions_v2/webui/test_webui_runtime.py
designs: []
design_note: 基于现有 WebUI 槽位和投递契约的缺陷修复，无独立设计文档。
---

# WebUI 槽位容量与投递回滚修复记录

## 现象与影响

Issue #7135 涉及标题、操作区和侧栏插件贡献的数量及布局边界。缺少槽位级容量约束时，多个实例可能挤占宿主空间；投递失败或任务取消后，已占用容量和 revision 必须恢复，避免后续请求受未成功投递的状态影响。

## 关联设计与当前契约

本次无独立 D 文档。当前槽位与展示规则见 `jiuwenswarm/extensions_v2/docs/features/F_06_render-service.md`、`F_07_ui-contributions.md` 和前端 `features/webui/docs/specs/S_01_components.md`。

## 根因与修复决策

- `conversation.header.title.after` 上限为 2，`conversation.header.actions` 和 `application.sidebar.footer` 各为 3；分别按会话或 Web 连接统计，前后端槽位目录保持一致。
- 满额时只拒绝新实例，已有实例仍可更新。拒绝不发送错误帧：新实例没有浏览器侧槽位，插件通过返回的 REJECTED 得知结果。CLEAR 不登记新实例，也不触发容量淘汰。
- 帧提交先推进 revision，再投递；失败或取消时通过 `finally` 恢复先前状态，并释放本次新建实例。对没有登记的临时 CLEAR 实例也恢复 revision；成功的 application CLEAR 则保留 revision，避免后续帧被前端清除标记过滤。
- 侧栏每个贡献的高度上限采用 48px，与侧栏入口的既有尺寸一致；超出部分仍由各实例裁剪。

## 回归证据与适用范围

2026-11-27，Windows 工作区候选（基于 `6a211621` 的本地改动）使用 `.venv/Scripts/python.exe -m pytest tests/unit_tests/extensions_v2/webui/test_webui_runtime.py tests/unit_tests/test_build_config.py -q -o addopts=''`：42 项通过。覆盖槽位满额拒绝、已有实例更新、取消释放容量，以及不存在的 application 实例 CLEAR 失败或取消后的 revision 恢复。版本同步测试也通过。

## 防错模式与资料同步

投递前更新的状态在失败与取消路径均需回滚，包含不进入活动实例表的临时实例；成功清除后的 application revision 仍需保留。槽位上限与布局契约同步到前后端现有文档。

## 遗留与后续

本次未做浏览器实际布局和键盘焦点轮廓检查；48px 容器仍裁剪超出边界的内容与轮廓，需要结合实际插件进行视觉验证。
