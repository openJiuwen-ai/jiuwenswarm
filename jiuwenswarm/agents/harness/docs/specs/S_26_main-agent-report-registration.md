# 主 Agent 深度研究报告登记

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_26 / main-agent-report-registration |
| 关联模块 | `common/tools/deepresearch`、`common/tools/deepresearch_plugin`、`common/rails/progressive_tool_rail.py` |
| 最近一次修订日期 | 2026-11-25 |

## 范围与不变量

OfficeAce 的 `deepresearch` 技能可在交付前登记主 Agent 自写的最终 Markdown。登记保留原路径、正文和相对资源链接。研究终态、文件发送和后续改写各自沿用原有入口。

## 接口和状态

`deepresearch_register_report(report_path)` 随 `deepresearch` 技能激活。工具取当前请求绑定的会话和工作区，只读取工作区内直接可访问的 UTF-8 Markdown 普通文件。成功返回 `status=registered`、`report_path`、`document_id` 和 `revision_id`，调用者随后发送该路径。

`<stem>.final-result.json` 保存报告快照，`<stem>.provenance.json` 保存现有 schema v2 的身份、哈希和初始版本。普通 Markdown 链接留在正文，快照的引擎引用白名单为空。相同正文重复登记返回已有身份。正文变更或快照冲突返回 `REVISION_CONFLICT`。部分写入失败时清理本次登记创建的文件。若只存在内容相同的快照，重试可完成登记。

## 执行路径

Skill 写出最终 Markdown → ProgressiveToolRail 校验 Skill 激活并绑定请求 → 登记工具校验工作区和文件身份 → 写入快照和版本记录 → Skill 发送原文件 → PC 预览读取同名版本记录。

## 验证

在未登记和已登记的同一份 Markdown 上调用正式改写工具，检查 `DOCUMENT_NOT_FOUND`、成功准备和版本提交。验证工作区绑定、技能可见性、中文路径、原文件字节、重复登记和故障回滚。
