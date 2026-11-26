# 主 Agent 深度研究报告登记

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_26 / main-agent-report-registration |
| 关联模块 | `jiuwenswarm/agents/harness/common/tools/deepresearch`、`jiuwenswarm/agents/harness/common/tools/deepresearch_plugin`、`jiuwenswarm/agents/harness/common/rails` |
| 最近一次修订日期 | 2026-11-26 |
| 关联 feature | [F_01_main-agent-report-rewrite.md](../features/F_01_main-agent-report-rewrite.md) |

## 范围 / 边界

本规约覆盖 OfficeAce 深度研究 Skill 交付主 Agent 自写的最终 Markdown 时，如何在当前请求的工作区内原位登记报告，以及后续预览和选区改写如何读取登记产物。SDK 生成的报告沿用自身发布流程。研究终态、证据充分性判定和后续格式导出各自沿用原有入口。

## 不变量

- **N1** 登记工具仅在 `deepresearch` 技能已激活且当前请求绑定了会话和输出工作区时执行。目标必须是工作区内可安全读取、内容非空的 UTF-8 Markdown 普通文件。
- **N2** 登记保留原文件路径、正文的精确字节和相对资源链接。只创建同名快照和版本记录，由 Skill 交付原 Markdown 路径。
- **N3** 同路径、同正文且现有版本记录与快照校验通过时，重复登记返回原有文档和版本 ID。正文、快照哈希或同名快照字节发生冲突时，返回 `REVISION_CONFLICT` 并保留已有产物。
- **N4** 首次登记先发布快照，再发布版本记录。若发布阶段出现可捕获的失败，按逆序清理本次创建的文件。登记完成前复读正文并比对字节。
- **N5** 若中断后只剩同名快照，重试以快照的精确字节比较决定是否续做。同一份正文生成的登记快照须保持确定性。字节一致时复用快照补齐版本记录，字节不同时保留它并报告冲突。
- **N6** 初始登记沿用现有 provenance schema v2，`parent_revision_id` 为空。选区改写通过现有准备和提交入口生成独立版本，并保留文档 ID 和父版本关系。

## 接口契约

| 接口 | 约定 |
|---|---|
| `deepresearch_register_report(report_path)` | 唯一入参为长度 1 至 4096 的路径字符串。相对路径以当前请求绑定的 `output_dir` 为基准。绝对路径同样须在该目录内。技能门控与请求上下文由运行时提供。 |
| 成功返回 | `status=registered`、`report_path`、`document_id`、`revision_id`。Skill 用返回的原路径发送文件。 |
| 预期失败返回 | `status=error`、`error_code`、`error`。触发条件见异常分支。 |
| 下游消费 | OfficeAce 预览按 Markdown 路径查找同名版本记录。选区改写继续调用 `deepresearch_prepare_rewrite` 和 `deepresearch_commit_rewrite`。 |

## 数据结构

| 产物 | 字段与用途 | 写入、读取和生命周期 |
|---|---|---|
| `<stem>.md` | UTF-8 正文。普通 Markdown 来源链接及相对资源链接随正文保留。 | 主 Agent 写入，登记工具有界读取。Skill 发送原路径，预览和改写读取正文。登记和改写均保留原稿。 |
| `<stem>.final-result.json` | `response_content` 为原正文，`response_content_type` 为 `text/markdown`，`citation_messages.data`、`infer_messages`、`chart_messages` 为空列表。序列化按键排序并生成紧凑的 UTF-8 JSON。 | 登记工具先写快照。重复登记和改写从版本记录定位快照并校验哈希。同字节的遗留快照可复用。失败清理仅处理本次创建的快照。 |
| `<stem>.provenance.json` | `schema_version=2`、`document_id`、`revision_id`、`parent_revision_id=null`。路径与完整性字段为 `markdown_path`、`content_sha256`、`final_result_path`、`final_result_sha256`，快照路径记录同目录文件名。 | 快照可用后由登记工具写入。预览用它识别报告版本，重复登记和改写据此检查原稿和快照。成功登记后与该版本共同保留，失败清理仅处理本次创建的版本记录。 |
| 版本记录中的补充字段 | `created_at`、`operation.action=main_agent_report_register`、`version_number`、`version_base_stem`。初始 `citations`、`inference_manifest`、`chart_manifest`、`rewrite_history` 为空列表。 | 版本编号和基名由 `artifact_naming.py` 的 `initial_version` 计算。创建时间和随机 ID 写入版本记录，供识别和追溯。 |

## 执行路径全景

1. **P1 写稿与接入。** RelayClaw 的 `office-claw-skills/deepresearch/SKILL.md` 让主 Agent 写出最终 Markdown，用写文件工具返回的路径调用登记工具。`common/tools/deepresearch/__init__.py` 的 `get_deepresearch_tools` 提供工具注册。
2. **P2 请求绑定。** `common/rails/progressive_tool_rail.py` 的 `before_tool_call` 检查技能门控，经 `_bind_deepresearch_context` 绑定请求，`after_tool_call` 释放上下文。`common/tools/deepresearch/rewrite_tools.py` 的 `deepresearch_register_report` 验证请求、会话和输出目录并调用登记实现。
3. **P3 校验与发布。** `common/tools/deepresearch_plugin/document_rewrite.py` 的 `register_main_agent_report` 归一化路径，在登记锁和授权目录内安全读取正文。`_register_existing_markdown` 验证已有版本，或用 `report_bundle.py` 的 `serialize_final_result_snapshot` 生成快照，再由 `_publish_create` 依次发布快照和版本记录。
4. **P4 交付与改写。** Skill 交付原 Markdown。RelayClaw 的 `MarkdownPreviewPanel.tsx` 加载同名版本记录，由 `deepresearch-rewrite-provenance.ts` 解析。用户选区后，`document_rewrite.py` 的 `prepare_rewrite` 校验版本和选区，`commit_rewrite` 发布独立的新版本。

## 异常分支与退出路径

| 触发条件 | 工具结果 | 退出与降级 |
|---|---|---|
| 技能尚未激活或旧引擎缺少登记工具 | 工具不可调用 | Skill 对工具不可用的情况尝试重新加载一次技能。仍不可用时，交付已保存的 Markdown 并提示暂不支持局部改写。 |
| 参数不合规，缺少活动请求、会话或输出目录，目标越出当前工作区、扩展名不符或正文为空 | `BAD_REQUEST` | 拒绝登记。 |
| 已登记正文哈希变化、重试时同名快照字节不同、版本记录指向的快照哈希不匹配，或发布后复读发现正文变化 | `REVISION_CONFLICT` | 保留已有文件。若本次已经创建产物，按 N4 回滚。 |
| 已有版本记录中的 Markdown 路径或文档、版本标识不合规 | `INVALID_PROVENANCE` | 保留已有版本文件并拒绝重新登记。 |
| 已有版本记录或其引用的快照缺失、不可读或无法解析，或快照引用数据不符合预期 | `DOCUMENT_NOT_FOUND` | 终止已有版本校验。快照路径越出工作区时返回 `BAD_REQUEST`。 |
| 正文读取、UTF-8 解码、序列化或发布中发生登记层捕获的 `OSError`、`ValueError` | `REPORT_REGISTRATION_FAILED` | 对本次创建的产物执行回滚。 |

登记工具返回失败时，RelayClaw Skill 继续交付已保存的 Markdown，并说明该文件暂不支持局部改写。

## 边界 case 清单

下表中的前五类以 `tests/unit_tests/agentserver/test_deepresearch_rewrite_tools.py` 为测试入口。

| 场景 | 当前行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| 中文路径和 CRLF 正文，登记后选择文字改写 | 登记原路径和原字节，生成仅替换选区的子版本。 | 路径编码或换行变化导致哈希不匹配。 | `test_main_agent_report_can_be_rewritten_after_registration` |
| 同正文重复登记，或登记后正文变化 | 分别返回原版本身份和 `REVISION_CONFLICT`。 | 重复建立版本或覆盖他人修改。 | `test_main_agent_report_can_be_rewritten_after_registration` |
| 请求无上下文、无输出目录，或目标在其他工作区 | 返回 `BAD_REQUEST`，不为越界文件创建版本记录。 | 误用进程目录或跨请求登记。 | `test_registration_is_limited_to_the_current_workspace_request` |
| 快照已发布，版本记录写入抛出异常 | 回滚本次产物，允许重试。 | 部分写入或误删原稿。 | `test_registration_rolls_back_an_incomplete_publication` |
| 中断后仅剩同名快照 | 一致时补齐版本记录，冲突时保留快照并返回错误。 | 序列化不稳定导致重试被拒，或覆盖原有快照。 | `test_registration_recovers_a_matching_orphan_snapshot`、`test_registration_preserves_a_conflicting_orphan_snapshot` |
| 技能激活前后调用登记工具 | 仅激活后可调用，按请求绑定并释放工作区上下文。 | 工具泄露到其他场景或沿用前一个请求的工作区。 | `test_skill_required_tool_visibility.py::test_report_registration_tool_requires_skill_and_binds_request_workspace` |

## 修改检查单

- 改 `serialize_final_result_snapshot` 或快照字段 → 检查序列化字节确定性，复跑遗留快照一致和冲突两条用例。
- 改 provenance schema、同名路径或哈希字段 → 连带检查登记、改写准备、改写提交及 OfficeAce 预览解析。
- 改请求工作区、技能门控或工具注册 → 连带检查 `before_tool_call` / `after_tool_call` 对称性，复跑工具可见性、注册清单和工作区边界用例。
- 改发布顺序、文件锁或失败清理 → 检查失败回滚的文件归属、遗留快照重试、重复登记和正文复读校验。
- 改 RelayClaw Skill 的调用时机或降级规则 → 验证登记成功后的原文件交付，以及旧引擎、登记失败时的文档交付。

## 与其它 spec 的关系

- [[S_06]] 覆盖 DeepResearch 四阶段 todo 快照。本规约覆盖最终报告的内容快照与版本记录。
- [[S_10]] 覆盖输入侧路径、文件读取和工作区额度守卫。本规约定义报告登记的请求工作区及其产物边界。
- [[S_24]] 覆盖 DeepResearch SDK 与 runner 日志。主 Agent 自写报告登记复用现有报告改写契约。
- [[S_25]] 覆盖运行时提示中技能目录的解析和宿主职责。本规约约束技能激活后的登记工具可见性、请求绑定和报告产物。
- RelayClaw 的 Skill 与 OfficeAce 预览是跨仓消费端，变更背景和发布顺序见 [F_01_main-agent-report-rewrite.md](../features/F_01_main-agent-report-rewrite.md)。
