# JiuwenMemory（SDK 接入）

> **目标**：帮助零基础读者从零开始，把 JiuwenSwarm 以 **SDK 模式**接入 agent-memory 记忆引擎，完成配置并投入使用。
>
> **英文版：** [English Version](../en/JiuwenMemory-SDK.md)

---

## 1. 概述

### 1.1 什么是 JiuwenMemory

JiuwenMemory 是 JiuwenSwarm 支持的一种**外接记忆 provider**，底层引擎是 agent-memory（mem2.0 内核）。它把对话中的关键信息写入结构化存储，并提供语义检索能力随时召回——让 Agent 拥有跨对话的持久记忆。

JiuwenMemory 提供两种接入方式：

| 模式 | 原理 | 适用场景 |
|------|------|----------|
| `server` | 远程 HTTP 调用 agent-memory server（`POST /v1/<verb>`） | 生产部署、多端共享同一个记忆服务 |
| `sdk` | 在 JiuwenSwarm 进程内**装配 agent-memory 内核**，直接调用，无 HTTP 跳转 | 单机嵌入、不想额外起一个 HTTP 服务、对延迟敏感 |

两种模式最终都落到同一个 `MemoryAPI`（`write` / `recall`），语义完全一致：`add` ≡ `api.write`，`search` ≡ `api.recall`。上层（Agent、记忆轨道）对 mode 无感。

> 本文**只讲 SDK 模式**。如需 server 模式，请参考[记忆系统](记忆.md#72-外接记忆配置)。

### 1.2 为什么用 SDK 模式

- **无需额外服务**：不必单独启动 agent-memory HTTP server，记忆内核与 JiuwenSwarm 同进程运行。
- **直连后端**：跳过 HTTP 层，调用路径更短、延迟更低。
- **配置收敛**：LLM 与 Embedding 模型**复用 JiuwenSwarm 顶层配置**，不必在记忆侧重复填写密钥。
- **统一语义**：与 server 模式暴露完全相同的两件工具（`mem2_search` / `mem2_add`）和同一块 system prompt，Agent 无感知切换。

### 1.3 工作原理简述

```
┌──────────────── JiuwenSwarm 进程 ────────────────┐
│  config.yaml                                     │
│    memory.engine: external        ← 总开关       │
│    memory.external.provider: jiuwenmemory        │
│    memory.external.jiuwen.mode: sdk ← 选 SDK     │
│  JiuwenMemoryProvider (mode=sdk)                 │
│    └─ assemble(config)  ← 进程内装配内核         │
│         ├─ api.write  ≡ 写记忆                   │
│         └─ api.recall ≡ 检记忆                   │
└─────────────────────┬────────────────────────────┘
                      │ 直连（无 HTTP）
        ┌─────────────┼─────────────┐
        ↓             ↓             ↓
   ┌─────────┐  ┌──────────┐  ┌──────────────┐
   │ KV 存储 │  │ 向量索引 │  │ 全文检索     │
   │ Redis   │  │ Milvus   │  │ Elasticsearch│
   └─────────┘  └──────────┘  └──────────────┘
```

SDK 模式会在进程内自动装配记忆内核。三个后端是记忆引擎的"存哪里"和"怎么查"：KV 存原始条目，向量索引做语义检索，全文检索做关键词匹配，三者混合得出最终结果。

> 💡 上面原理看不懂没关系，日常使用只需填好配置文件，剩下的 JiuwenSwarm 全部自动处理。

---

## 2. 内置记忆与 JiuwenMemory 的区别

JiuwenSwarm 默认用**内置记忆**（`memory.engine: builtin`），基于本地 Markdown 文件；切到 JiuwenMemory 则是远程结构化后端。两者最本质的差别不在"工具名"，而在**记忆如何被触发**：

- **内置记忆**：没有自动触发机制。Agent 需要在对话中**主动调用** `memory_*` 工具（`memory_search` / `write_memory` 等）来检索或写入记忆，不调用就不会发生记忆行为。
- **JiuwenMemory**：由 **ExternalMemoryRail（记忆轨道）自动驱动**。每轮对话开始时自动检索相关记忆注入上下文、每轮结束时自动沉淀当轮对话——**不一定调用任何工具**，这些发生在 Agent 视线之外。

| 对比项 | 内置记忆（builtin） | JiuwenMemory（SDK 模式） |
|--------|---------------------|--------------------------|
| 存储位置 | 本地 Markdown 文件（`MEMORY.md` / `USER.md` / 每日日志） | 远程后端（Redis / Milvus / Elasticsearch） |
| 记忆触发方式 | **Agent 主动调工具**，无自动机制 | **Rail 自动触发**（prefetch 注入 + sync_turn 沉淀），工具可选 |
| 每轮开始 | 无自动动作 | Rail 自动 `prefetch` 检索，把命中记忆注入上下文 |
| 每轮结束 | 无自动动作 | Rail 自动 `sync_turn`，把当轮对话沉淀入库（带熔断器） |
| Agent 可调的工具 | `memory_search` / `write_memory` / `edit_memory` / `read_memory` 等 `memory_*` | `mem2_search` / `mem2_add` 等 `mem2_*`（可选，作为补充手段） |
| 抽取/去重 | 靠 Dreaming 离线整理或 Agent 手动归纳 | `infer_turns: true` 时 sync_turn 自动用 LLM 蒸馏成去重事实 |
| 数据可见性 | 用户可直接打开 `memory/` 目录查看、编辑 Markdown 文件 | 数据在远程后端，不可直接看文件，靠召回 |

### 2.1 JiuwenMemory 的 Rail 自动触发流程

ExternalMemoryRail 挂载后，每轮对话自动走这三步（用户无感）：

```
before_invoke（每轮开始）
  └─ 首次调用时 initialize provider（仅一次）

before_model_call（模型调用前）
  └─ prefetch(用户本轮 query)          ← 自动检索记忆
       超时 5s，失败/无命中则跳过
  └─ 命中内容包成 <memory-context> 块注入上下文
       （系统标记"recalled memory, NOT new user input"，防混淆）

after_invoke（每轮结束）
  └─ sync_turn(用户 query, 助手输出)    ← 自动沉淀当轮对话
       串行化执行 + 熔断器（连续失败 5 次开 120s 冷却）
       心跳/cron 等后台运行时跳过
```

注入到上下文的记忆块长这样（Agent 会看到但知道这是召回的记忆，不是用户新输入）：

```xml
<memory-context>
[System note: recalled memory context from long-term memory, NOT new user input.]

- 用户喜欢吃辣
- 用户身在上海
</memory-context>
```

> 💡 所以用 JiuwenMemory 时，**即使一轮对话里 Agent 没调用任何 `mem2_*` 工具，记忆的检索和沉淀照样发生了**——这就是 rail 自动驱动的效果。工具（`mem2_search`/`mem2_add`）只是给 Agent 一个额外的、显式检索/写入的补充手段，不调用也没关系。

### 2.2 怎么判断当前用的是哪种

观察对话的**行为特征**，而不只是看工具名：

- 用的是**内置记忆**：会在对话里看到 Agent **主动调用** `memory_search` / `write_memory` / `edit_memory` / `read_memory` 等 `memory_*` 工具，且 `memory/` 目录下的 Markdown 文件被改动。不调用 = 没有记忆行为。
- 用的是 **JiuwenMemory**：上下文里出现 `<memory-context>` 注入块（每轮自动），但不一定有 `mem2_*` 工具调用——沉淀靠 rail 自动完成。若看到 `mem2_*` 工具调用，是 Agent 主动做的显式补充。

例如同一个"记住我喜欢吃辣"的请求，沉淀路径完全不同：

```
# 内置记忆：Agent 必须主动调工具写入
[tool: write_memory]  path=memory/USER.md, content=...喜欢吃辣...
（不调用就什么都不存）

# JiuwenMemory：本轮结束时 rail 自动沉淀，Agent 无感
（无需工具调用；sync_turn 自动把"喜欢吃辣"蒸馏入库）
（可选：Agent 也可主动调 [tool: mem2_add] 做显式补充写入）
```

> 💡 两种记忆可独立使用，也可用 `memory.engine: both` 同时启用——此时内置工具 `memory_*` 和外接 rail 自动触发并存。本文讲的是切到 JiuwenMemory（`external`）的用法。

---

## 3. 前置条件

### 3.1 主程序能正常对话

JiuwenSwarm 已安装并能正常启动对话。记忆是挂在对话之上的能力，主程序要先跑通。如未安装，请先参考[安装指南](安装指南.md)。

### 3.2 安装 agent-memory 内核

```bash
pip install JiuwenMemory
```

验证：

```bash
python -c "from api import assemble; print('agent-memory OK')"
```

> ⚠️ 若未装或装错环境，启动时日志会出现 `RuntimeError` 提示安装 `JiuwenMemory`，记忆轨不挂载（主流程不阻塞）。如果你用 `uv`/虚拟环境启动，务必在**同一个环境**里执行 `pip install`。

### 3.3 部署三个后端服务

SDK 模式需要三个后端服务（KV / 向量 / 全文），请先部署并确认地址可达：

| 后端 | 作用 | 实现 |
|------|------|------|
| KV 存储 | 存记忆原始条目 | Redis |
| 向量索引 | 语义检索（按意思找） | Milvus |
| 全文检索 | 关键词匹配（按词找） | Elasticsearch |

> 三个都用远程服务，数据可靠、可长期使用。三个后端的 `type`/`url` 在 §4.2 的配置中填写。

---

## 4. 配置方法（两步走）

> 找到 `config.yaml`（通常在 `~/.jiuwenswarm/config/config.yaml`）。只需两步。

### 4.1 第一步：确认大模型与 Embedding 配置

**这是最易忽略的一步。** SDK 模式下，记忆的 LLM 抽取和 Embedding 向量化**复用 JiuwenSwarm 顶层配置**，不在 `jiuwen` 段重复填：

- **LLM**：`infer_turns: true` 时把对话蒸馏成去重事实。来源是顶层 `models.defaults` 中 `is_default: true` 的模型。
- **Embedding**：把文本转向量做语义检索。来源是顶层 `embed` 段。

能正常对话说明 LLM 基本配好了，但要确认 **Embedding 也配了**（很多人只配了主对话模型）。确认顶层有这两段（通常默认就有）：

```yaml
models:
  enable_free_models: true
  defaults:
    - model_client_config:
        api_base: ${API_BASE}
        api_key: ${API_KEY}
        model_name: ${MODEL_NAME}
        client_provider: ${MODEL_PROVIDER}
      model_config_obj:
        temperature: 0.95
      is_default: true               # SDK 模式认这个 is_default 的模型

embed:
  embed_api_key: ${EMBED_API_KEY}
  embed_base_url: ${EMBED_API_BASE}
  embed_model: ${EMBED_MODEL}
```

对应环境变量（写在 `.env` 或系统环境里）：

```bash
# LLM（主对话用，SDK 模式记忆抽取也复用它）
export API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
export API_KEY=sk-your-llm-key
export MODEL_NAME=qwen-plus
export MODEL_PROVIDER=openai

# Embedding（SDK 模式语义检索必需，三个缺一不可）
export EMBED_API_KEY=sk-your-embed-key
export EMBED_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
export EMBED_MODEL=text-embedding-v3
```

> ⚠️ `EMBED_*` 三个变量缺一不可。只配了 `API_*`（主对话能跑）没配 `EMBED_*`，记忆检索不会工作。
>
> Embedding 模型输出维度必须与 §4.2 的 `embedder_dim` 一致，常见对应见 §6.2。

### 4.2 第二步：配置 config.yaml

在 `memory` 段填入以下配置（远程三服务方案）：

```yaml
memory:
  engine: external                         # 仅外接（both = 内置+外接并存）
  external:
    provider: jiuwenmemory                # 选 JiuwenMemory 这家
    user_id: __default__                  # 数据隔离标识（= mem2 的 scope）
    scope_id: __default__
    jiuwen:
      mode: sdk                           # 关键：用 SDK 模式
      tenant_id: default                  # org 轴
      infer_turns: true                   # 对话轮次走 LLM 抽取+去重
      save_assistant_turns: false         # 默认只存用户轮
      sdk:
        kv_type: redis
        kv_url: redis://localhost:6379/0        # 你的 Redis 地址
        vector_type: milvus
        vector_url: http://localhost:19530      # 你的 Milvus 地址
        db_type: elasticsearch
        db_url: http://localhost:9200           # 你的 Elasticsearch 地址
        embedder_dim: 1024                      # 须与向量库 dim 及 Embedding 输出一致
```

把三个 `*_url` 换成你实际部署的服务地址即可。

> 💡 如果你倾向用环境变量管理（不直接改 `config.yaml`），SDK 模式所有字段都支持环境变量覆盖。`config.yaml` 里只需：
>
> ```yaml
> memory:
>   engine: external
>   external:
>     provider: jiuwenmemory
>     jiuwen:
>       mode: sdk
> ```
>
> 其余靠环境变量：`MEMORY_ENGINE` / `MEMORY_EXTERNAL_PROVIDER` / `JIUWEN_MEMORY_MODE` / `JIUWEN_KV_TYPE`+`JIUWEN_KV_URL` / `JIUWEN_VECTOR_TYPE`+`JIUWEN_VECTOR_URL` / `JIUWEN_DB_TYPE`+`JIUWEN_DB_URL` / `JIUWEN_EMBEDDER_DIM`（完整列表见 §5.3）。

---

## 5. 字段说明

### 5.1 公共字段（两模式通用）

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `mode` | `server` | `sdk` 表示进程内装配内核 |
| `tenant_id` | `default` | Scope 的 org 轴，用于数据隔离 |
| `user_id` | `__default__`（顶层 `memory.external.user_id`） | Scope 的 user 轴（= mem2 的 scope 串） |
| `infer_turns` | `true` | `sync_turn` 是否把用户轮走 LLM 抽取+去重 |
| `save_assistant_turns` | `false` | `sync_turn` 是否也存助手轮（默认只存用户轮） |

> `user_id` 一般由记忆轨在 `initialize()` 时从顶层 `memory.external.user_id` 注入，`jiuwen.user_id` 只是与其他 provider 对齐留的显式覆盖位。

### 5.2 SDK 专用字段（`memory.external.jiuwen.sdk`）

只需配置三个后端的 `type` + `url`，LLM/Embedder 复用顶层：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `kv_type` | `redis` | KV 后端类型：`sqlite` / `redis` / `memory` |
| `kv_url` | `redis://localhost:6379/0` | `sqlite`=文件路径；`redis`=连接串 |
| `vector_type` | `milvus` | 向量后端：`milvus` / `memory` |
| `vector_url` | `http://localhost:19530` | milvus 服务地址 |
| `db_type` | `elasticsearch` | 全文后端：`elasticsearch` / `memory` |
| `db_url` | `http://localhost:9200` | elasticsearch 地址 |
| `embedder_dim` | `1024` | 向量维度，须与向量库 dim 及 Embedding 模型输出一致 |

### 5.3 环境变量总表

| 环境变量 | 说明 |
|----------|------|
| `MEMORY_ENGINE` | 总开关：`external` / `both` / `builtin` / `none` |
| `MEMORY_EXTERNAL_PROVIDER` | Provider 名称，选 `jiuwenmemory` |
| `MEMORY_USER_ID` | 数据隔离标识 |
| `MEMORY_SCOPE_ID` | Scope 标识 |
| `JIUWEN_MEMORY_MODE` | `server` / `sdk` |
| `JIUWEN_MEMORY_TENANT_ID` | tenant_id |
| `JIUWEN_KV_TYPE` / `JIUWEN_KV_URL` | KV 后端类型与地址 |
| `JIUWEN_VECTOR_TYPE` / `JIUWEN_VECTOR_URL` | 向量后端类型与地址 |
| `JIUWEN_DB_TYPE` / `JIUWEN_DB_URL` | 全文后端类型与地址 |
| `JIUWEN_EMBEDDER_DIM` | 向量维度 |

### 5.4 由 Builder 写死的配置项

以下开关在 SDK 模式下由 builder 缝合时**写死**，不在 `config.yaml` 暴露，无需也不能配置：

| 配置项 | 值 | 原因 |
|--------|----|------|
| `rerank_enabled` | `false` | 不开放外部 reranker |
| `graph_enabled` | `false` | 不开放图谱 |
| `enable_thinking` | `false` | 关闭思考链 |
| `embedder_ssl_verify` | `false` | 关闭 Embedding SSL 校验 |
| `tokenizer.default.target` | `jieba` | 中文 BM25 分词 |

---

## 6. 后端选择进阶

### 6.1 后端 type/url 对照

#### KV 存储（`kv_type` / `kv_url`）

| `kv_type` | `kv_url` 格式 | 说明 |
|-----------|---------------|------|
| `redis` | `redis://host:port/db` | 远程 Redis 连接串 |
| `sqlite` | 文件路径（如 `./agent_memory.db`） | 本地 SQLite 文件；若填了 `redis://...` 会自动回落 `agent_memory.db` |
| `memory` | 无需填 | 进程内字典，重启丢失 |

#### 向量索引（`vector_type` / `vector_url`）

| `vector_type` | `vector_url` 格式 | 说明 |
|---------------|---------------------|------|
| `milvus` | `http://host:19530` | 远程 Milvus 服务地址 |
| `memory` | 无需填 | 进程内暴力余弦相似度，仅适合小数据量 |

#### 全文检索（`db_type` / `db_url`）

| `db_type` | `db_url` 格式 | 说明 |
|-----------|---------------|------|
| `elasticsearch` | `http://host:9200` | 远程 Elasticsearch 地址 |
| `memory` | 无需填 | 进程内倒排索引，重启丢失 |

### 6.2 向量维度对照

`embedder_dim` 必须同时满足：

1. 与 Milvus collection 的 `dim` 一致（builder 会写入 `params.dim`）
2. 与所配 Embedding 模型的实际输出维度一致

常见 Embedding 模型与维度对应：

| Embedding 模型 | 典型维度 | `embedder_dim` 设为 |
|----------------|----------|---------------------|
| 通义 text-embedding-v3 | 1024 | `1024` |
| OpenAI text-embedding-3-small | 1536 | `1536` |
| OpenAI text-embedding-3-large | 3072 | `3072` |

> ⚠️ 维度不匹配会导致写入或检索失败。若 Milvus 中已存在旧 collection 且维度不符，需先清理旧 collection 再重启。

---

## 7. 启动与验证

### 7.1 启动 JiuwenSwarm

按平时方式启动（命令行或桌面端均可）。

### 7.2 检查挂载成功

查看日志（路径见[日志](日志.md)），寻找关键行：

```
[ExternalMemoryBuilder] JiuwenMemory provider built (mode=sdk, tenant=default)
```

看到这行说明 SDK 模式记忆轨已成功装配。**没有这行 = 没挂上**，回查 §10.1。

### 7.3 验证记忆生效

在对话中测试写入与召回：

```
User: 记住我喜欢吃辣
Assistant: 好的，已记住您喜欢吃辣。
[mem2_add 工具被调用，记忆写入]

# 新开会话或隔几轮后
User: 我有什么饮食偏好？
Assistant: 根据记忆，您喜欢吃辣。
[mem2_search 工具被调用，召回相关记忆]
```

能看到 Agent 主动调用 `mem2_search` / `mem2_add` 工具并跨会话召回信息，即配置成功。

### 7.4 安装自检命令

挂载失败时先跑这两条排查：

```bash
# 1. 确认 agent-memory 内核已装
python -c "from api import assemble; print('agent-memory OK')"

# 2. 确认顶层 Embedding 配置生效（SDK 模式必需）
python -c "import os; print('EMBED_API_KEY:', bool(os.environ.get('EMBED_API_KEY'))); print('EMBED_API_BASE:', os.environ.get('EMBED_API_BASE')); print('EMBED_MODEL:', os.environ.get('EMBED_MODEL'))"
```

三个 `EMBED_*` 都有值才算配好。

---

## 8. 使用方式

### 8.1 Agent 自动调用（推荐）

配置完成后无需手动操作。JiuwenMemory 对外暴露**两件工具**与一块 system prompt，Agent 会自动在对话中调用：

| 工具 | 作用 |
|------|------|
| `mem2_search` | 语义检索记忆，按 query 召回相关条目 |
| `mem2_add` | 写入一条记忆，支持 `infer` 参数控制是否走 LLM 抽取去重 |

两个工具在 SDK 与 server 模式下接口完全相同，Agent 无感知。

### 8.2 对话轮次自动沉淀（sync_turn）

每轮对话结束时，记忆轨会调用 `sync_turn` 自动沉淀当轮内容：

- `infer_turns: true`（默认）：用户轮被 LLM **蒸馏成去重事实**存储，避免冗余。
- `infer_turns: false`：用户轮**原文存储**。
- `save_assistant_turns: true`：同时存助手轮（助手轮永远原文存，不抽取）。

> 💡 默认 `save_assistant_turns: false`——助手回复通常可从用户轮推导、价值较低，只存用户轮更高效。希望完整保留对话原文则设为 `true`。

### 8.3 工具调用返回示例

`mem2_add` 写入返回：

```json
{
  "result": "stored",
  "item_id": "mem-abc123",
  "content": "用户喜欢吃辣",
  "tier": "HOT"
}
```

- `result`: `stored`（新写入）或 `deduped`（infer 去重后判定为重复，未新增）
- `item_id`: 为空时表示被去重，`result` 为 `deduped`

`mem2_search` 检索返回：

```json
{
  "results": [
    {"content": "用户喜欢吃辣", "item_id": "mem-abc123", "score": 0.92}
  ],
  "count": 1
}
```

---

## 9. 接入链路详解（进阶）

> 本节面向想理解内部机制的读者，日常使用可跳过。

### 9.1 装配阶段

```
build_external_memory_rail(config)
  └─ _build_jiuwen_provider(ext_cfg, config)
       └─ mode == "sdk"
            ├─ _build_jiuwen_sdk_config_dict(sdk_cfg, full_config)
            │    ├─ kv_store      = _kv_spec(kv_type, kv_url)
            │    ├─ vector_store  = _vector_spec(vector_type, vector_url, dim)
            │    ├─ fulltext_store = _fulltext_spec(db_type, db_url)
            │    ├─ globals = {embedder_dim, chunk_size, rerank_off, ...}
            │    ├─ llm/embedder 凭证 ← 顶层 models.defaults / embed
            │    └─ 返回 {globals, tokenizer, kv_store, vector_store, fulltext_store, [llm], [embedder]}
            └─ JiuwenMemoryProvider(mode=sdk, config_dict=..., tenant_id, user_id, ...)
```

### 9.2 初始化与调用阶段

```
_SDKBackend.initialize()
  ├─ from api import assemble
  ├─ cfg = Config.from_dict(config_dict)   # 空字典或失败回落默认，仅 warn
  ├─ self._api = assemble(config=cfg)       # 失败抛 RuntimeError，被 builder 兜住
  └─ 缓存 Scope / Modality / Context / DisclosureLevel 类型

search(query, top_k):
  ├─ scope = Scope(org=tenant_id, user=user_id)
  ├─ ctx = Context(scope)
  └─ asyncio.to_thread(api.recall, query, ctx, identity=scope, top_k=k, disclosure=L2)
       # api.recall 同步、内部 asyncio.run()，故丢到工作线程

add(content, infer, tags):
  ├─ metadata = {"infer": "true"} if infer else {}
  ├─ modality = Modality.TEXT
  └─ asyncio.to_thread(api.write, content, scope, modality, identity=scope, tags, metadata)
```

### 9.3 失败降级原则

任何装配/调用失败，记忆轨都**不阻塞主流程**：

- agent-memory 未安装 → `RuntimeError` 被 builder 兜住，记忆轨返回 `None`，对话照常进行。
- `Config.from_dict` 失败 → 回落内置默认配置，仅 warn。
- 单次读写异常 → 返回空结果（`search` → `[]`，`add` → `None`），不抛错。

---

## 10. 常见问题

### 10.1 启动报 "pip install JiuwenMemory"

**问题描述**：日志出现 `RuntimeError` 提示安装 `JiuwenMemory`，记忆轨没挂。

**解决方案**：当前 Python 环境未装 agent-memory 内核。执行 `pip install JiuwenMemory`，用 §7.4 命令验证。**最常见原因**：用 `uv`/虚拟环境启动 JiuwenSwarm，却在系统 Python 里装了内核——两者必须是同一个环境。

### 10.2 记忆轨没有挂载（找不到 built 日志）

**问题描述**：配置了 `provider: jiuwenmemory`，但日志里没有 `JiuwenMemory provider built` 这行。

**排查步骤**：

1. 确认 `memory.engine` 是 `external` 或 `both`，而非默认的 `builtin`。
2. 确认 `memory.external.provider` 是 `jiuwenmemory`（小写、无空格、无多余字符）。
3. 确认 `jiuwen.mode` 是 `sdk`。
4. 确认 `pip install JiuwenMemory` 已在正确环境执行（§7.4 验证）。
5. 查看日志是否有更靠前的报错（如内核装配失败）。

### 10.3 LLM 抽取失败或记忆没被提炼

**问题描述**：`infer_turns: true` 但记忆没被蒸馏成事实，都是原文。

**解决方案**：SDK 模式下 LLM 复用顶层 `models.defaults[is_default=true]`。确认顶层已配置可用 LLM（`api_base` / `api_key` / `model_name` 齐全）且模型可正常调用（主对话能跑说明通常没问题）。LLM 不可用时 `infer` 会降级，记忆仍会原文存储，不会丢，只是没去重。

### 10.4 语义检索不工作 / 报维度错误

**问题描述**：检索不返回结果，或日志报向量维度不匹配。

**解决方案**：`embedder_dim` 必须同时与 Milvus collection 维度、Embedding 模型输出维度一致。

1. 确认 `EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL` 三个环境变量都配了（§7.4 验证）。
2. 按 §6.2 核对 Embedding 模型实际输出维度，调整 `embedder_dim`。
3. 若 Milvus 已存在旧 collection 维度不符，先清理旧 collection 再重启。

### 10.5 数据隔离如何实现

**解决方案**：JiuwenMemory 通过 `Scope(org=tenant_id, user=user_id)` 实现隔离：

- `tenant_id`：org 轴，通常代表组织/租户。
- `user_id`：user 轴，即 mem2 的 scope 串，由顶层 `memory.external.user_id` 注入。

不同 `(tenant_id, user_id)` 组合的记忆相互不可见。多用户场景下给每人设不同 `user_id` 即可隔离。

---

## 11. 快速决策清单

按这个清单走即可完成接入：

- [ ] 主程序 JiuwenSwarm 能正常对话（前提）
- [ ] `pip install JiuwenMemory`，并 `python -c "from api import assemble"` 验证
- [ ] 配好顶层 `EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL`（§4.1，SDK 必需）
- [ ] 部署 Redis / Milvus / Elasticsearch 三服务并确认地址可达（§3.3）
- [ ] 在 `config.yaml` 的 `memory` 段填好 SDK 配置，三个 `*_url` 换成实际地址（§4.2）
- [ ] `embedder_dim` 与你的 Embedding 模型维度对齐（§6.2）
- [ ] 启动，日志看到 `JiuwenMemory provider built (mode=sdk, ...)`（§7.2）
- [ ] 对话测试：让 Agent 记一件事，隔几轮/换会话再问（§7.3）

---

## 返回导航

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)
