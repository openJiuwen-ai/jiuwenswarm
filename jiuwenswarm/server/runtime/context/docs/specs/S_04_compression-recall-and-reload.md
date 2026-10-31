# 压缩召回与消息重载

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_04 / compression-recall-and-reload |
| 关联模块 | react.context_engine_config + agent-core CompressionRecall + compression_recall tool |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

### 本规约管什么

- **compression_recall_config**：压缩时归档原文分块、模型侧 `[[COMPRESSION_RECALL: id=...]]` 标记与 **`recall_compressed_context`** 工具。
- **enable_reload**：卸载大消息后通过 **`[[HANDLE:xxx]]`** 把原文拉回上下文（MessageSummaryOffloader 等配合）。
- 二者 **独立门控**、独立产品语义。

### 本规约不管什么

- Offloader 内部摘要 prompt → agent-core processor 实现。
- PersonalContext 蒸馏召回 → `server/personal_context/`。

## 不变量

- **N1（独立开关）**：`compression_recall_config.enabled` 与 `enable_reload` 在 `_deep_agent_context_engine_config` 中分别映射；Good：只开 reload 不强制落盘 recall 归档。Bad：用一个 yaml 键隐式绑定两者。
- **N2（召回工具名）**：agent-core 工具名为 **`recall_compressed_context`**（`CompressionRecallTool`）；产品文档与 spec 引用此名，不发明别名。
- **N3（归档位置）**：config 注释约定会话目录下 `compression_recall/` 分块存储（chunk_size / overlap 来自 config）；未 enabled 时不应依赖该目录存在。
- **N4（适用压缩器）**：config 注释：压缩召回**仅对**对话/当前轮/轮次级压缩器生效——改适用面时需同时改 yaml 注释与 core 行为核对。
- **N5（protected 工具输出）**：`message_summary_offloader_config.protected_tool_names` 默认含 `read_file` / `read_pdf` / `skill_tool`——这些工具输出保持原文，不卸载；改名单会影响窗口占用与召回素材来源。

## 接口契约

### config.yaml（`react.context_engine_config`）

| 键 | 默认（产品 config） | 语义 |
|---|---|---|
| `enable_reload` | true | 允许 `[[HANDLE]]` 重载 |
| `compression_recall_config.enabled` | false | 开启召回归档 |
| `compression_recall_config.chunk_size_tokens` | 3000 | 分块大小 |
| `compression_recall_config.chunk_overlap_tokens` | 500（yaml）/ 代码 fallback 300 | 重叠；须 `< chunk_size` |

`_deep_agent_context_engine_config` 对 recall 的 overlap 使用 `parse_int(..., 300)`——以代码 fallback 为准时与 yaml 注释不一致应修 yaml 或 parse 默认值（改时同步本 spec）。

### Core 工具（协作）

- 名称：`recall_compressed_context`
- 路径：`openjiuwen/harness/tools/compression_recall.py`
- 前置：`CompressionRecallConfig.enabled` + 已有归档块

### 模型标记（协作）

- `[[COMPRESSION_RECALL: ...]]`：召回路径
- `[[HANDLE: ...]]`：reload 路径（enable_reload）

## 数据结构

- **compression_recall/**：按 session 归档；块 id 由 core 生成（细节见 core 单测 `test_compression_recall_tool.py`）。
- **Offload 索引**：`[[OFFLOAD:...]]` 保留在窗口内指向卸载内容（enable_reload 时 HANDLE 解析）。

## 执行路径全景

### P1：配置进入 Engine

```
create_deep_agent(..., context_engine_config=_deep_agent_context_engine_config)
  → CompressionRecallConfig(enabled, chunk_size_tokens, chunk_overlap_tokens)
  → enable_reload 布尔
```

### P2：压缩时归档（enabled）

```
Dialogue / CurrentRound / RoundLevel compressor 替换消息
  → core 按 CompressionRecallConfig 写 compression_recall/
```

### P3：模型召回

```
模型输出 COMPRESSION_RECALL 标记或调用 recall_compressed_context
  → core 读归档注入窗口
```

### P4：HANDLE 重载

```
enable_reload 且 offloader 已卸载
  → 模型 HANDLE 标记 → core reload 原文到窗口
```

## 修改检查单

- 改 chunk 参数或 enabled 语义 → `config.yaml` + `_deep_agent_context_engine_config` + 本 spec + core 单测。
- 新增第四种参与召回的 compressor → 核对 core 实现后更新 N4 与 config 注释。
- 暴露给前端的「上下文召回」能力 → 区分 COMPRESSION_RECALL vs HANDLE，避免 UX 混称。

## 与其它 spec 的关系

- [[S_02]]：CompressionRecallConfig 与 processor 链
- [[S_06]]：config 单一真相源
- [[S_07]]：是否在 system prompt 中提示召回/重载用法（若由 prompt section 承担，在 S_07 登记）
