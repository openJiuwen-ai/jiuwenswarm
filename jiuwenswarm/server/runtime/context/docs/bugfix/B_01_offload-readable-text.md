# 卸载产物超过 read_file 读取限制时模型无法读回

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-25 |
| 范围 | `resources/config.yaml`（`enable_reload` 注释）、`docs/specs/S_04_compression-recall-and-reload.md`（N6、P4）；实现在 agent-core |
| 实现归档 | agent-core `openjiuwen/core/context_engine/docs/bugfix/B_01_offload-readable-text.md`（根因、决策、拒绝的方案、验证） |
| 关联 spec | [[S_04]] N6、P4；agent-core `core/context_engine/docs/specs/S_05_compression-recall-and-reload.md` 不变量 5、7–10 |

## 复现场景

会话 `officeclaw_760d72aced87f973a15d5663`（2026-10-22，采集包 `office_claw_log_20261022_110021`）：模型用 bash `cat` 读取约 70 万字符的日志并传 `max_output_chars=0`，输出整段进入工具消息，被 `MessageSummaryOffloader` 卸载。模型按 marker 读取 `MessageSummaryOffloader_<handle>.json`，`read_file` 返回 `File content (710.5KB) exceeds maximum allowed size (256KB)`，卸载内容未能读回。

## 根因

卸载文件以 JSON 写盘，正文换行被转义，整段原文成为一个物理行；`read_file` 的整文件大小检查与单页 token 检查、`grep` 的 `--max-columns` 都无法处理该行。详见实现归档。

## 修复口径

- agent-core：卸载时在 `.json` 旁写同名 `.txt`（保留真实换行、超长行折行），marker path 指向 `.txt`，marker 前附读取提示；写盘检查返回码；reload 提示改为先搜索再分页。
- jiuwenswarm：无代码改动，不打临时补丁，随 agent-core 依赖升级生效；`config.yaml` 中 `enable_reload` 注释由已废弃的 `[[HANDLE:xxx]]` 改为 `[[OFFLOAD: …, path=…]]`；S_04 新增 N6（卸载读回产物）并改写 P4。
- 不改 bash / powershell / `mcp_exec_command` 的输出落盘逻辑，不改 `read_file` / `grep`。
