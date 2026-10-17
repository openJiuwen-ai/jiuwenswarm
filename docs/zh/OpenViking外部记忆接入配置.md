# OpenViking 外部记忆接入配置

> **目标**：帮助读者将 OpenViking 配置为 JiuwenSwarm 的外部记忆后端，掌握模型配置、服务启动、数据目录迁移与连通性验证方法。
> 本文档同时覆盖 **本地配置方式**（单机、`127.0.0.1`、dev 模式）与 **联网配置方式**（远端服务、API Key 鉴权）两种场景。

---

## 1. 概述

OpenViking 是字节跳动开源的自进化上下文数据库，可提供 Agent 场景的
文档入库、语义检索与上下文沉淀能力。本文档说明如何将 OpenViking 接入
JiuwenSwarm 作为外接记忆 provider，并覆盖 Windows 环境下已暴露的常见问题。

**两种配置方式适用场景：**

| 方式     | 适用场景                     | 服务地址                | 鉴权                           |
| -------- | ---------------------------- | ----------------------- | ------------------------------ |
| 本地配置 | 单机开发/个人使用            | `http://127.0.0.1:1933` | dev 模式，无需 Key             |
| 联网配置 | 多端同步、团队协作、远端部署 | `https://<远端地址>`    | API Key 鉴权（`root_api_key`） |

接入后的数据流如下：

```
用户提问 ──► JiuwenSwarm（memory.engine=external/both）
                  │
                  ▼
          OpenViking HTTP Server
                  │
        ┌─────────┼─────────────┐
        ▼         ▼             ▼
   文档入库    语义检索     概览/摘要生成
（temp_upload （search/find） （VLM 模型）
 +add_resource）
```

**项目参考地址：**

- OpenViking 官方文档：https://docs.openviking.ai/
- OpenViking 仓库：https://github.com/volcengine/OpenViking
- JiuwenSwarm 记忆系统文档：[记忆.md](./记忆.md)

---

## 2. 前置操作

| 序号 | 前置项                   | 说明                                                                                   |
| ---- | ------------------------ | -------------------------------------------------------------------------------------- |
| 1    | 安装 OpenViking          | `pip install --upgrade openviking`，验证 `openviking-server --version`                 |
| 2    | 开通 Embedding 模型      | 火山方舟控制台开通 embedding 模型（如 `doubao-embedding-vision-251215`），获取 API Key |
| 3    | 开通 VLM 模型            | 火山方舟控制台开通 VLM 模型（如 `doubao-seed-2-0-lite-260428`），获取 API Key          |
| 4    | 确认端口可用             | 默认 `127.0.0.1:1933`，如被占用可调整 `ov.conf` 的 `server.port`                       |
| 5    | （联网方式）准备 API Key | 服务端配置 `server.root_api_key`，客户端使用对应 User/Admin Key                        |

> **注意**：VLM 模型必须先开通再启动服务，否则解析文档生成概览时会返回
> `404 ModelNotOpen`，并可能连锁导致服务崩溃（详见第 5 节）。

---

## 3. 具体配置步骤

### 3.1 编写 OpenViking 服务端配置（ov.conf）

配置文件位于 `~/.openviking/ov.conf`，完整示例（以火山方舟模型为例）：

```json
{
  "storage": {
    "workspace": "D:\\OpenVikingData"
  },
  "embedding": {
    "dense": {
      "provider": "volcengine",
      "model": "doubao-embedding-vision-251215",
      "api_key": "<EMBEDDING_API_KEY>",
      "api_base": "https://ark.cn-beijing.volces.com/api/v3",
      "dimension": 1024
    }
  },
  "vlm": {
    "provider": "volcengine",
    "model": "doubao-seed-2-0-lite-260428",
    "api_base": "https://ark.cn-beijing.volces.com/api/v3",
    "api_key": "<VLM_API_KEY>"
  },
  "server": {
    "host": "127.0.0.1",
    "port": 1933,
    "root_api_key": "<联网方式必填；本地 dev 模式删除此行>"
  }
}
```

> **本地方式**：不配置 `server.root_api_key`，服务自动进入 dev 模式，客户端无需 Key。
> **联网方式**：配置 `server.root_api_key`，服务自动进入 api_key 模式；
> 通过 Admin API 创建 User Key 供客户端使用。
>
> 配置写入建议通过配置工具或 `ask_user` 交互完成，避免将 API Key 直接
> 输入命令行历史；回显时应做脱敏。

### 3.2 启动服务（Windows UTF-8 规避）

Windows 中文控制台默认 GBK 编码，rich 库输出 `✓` 会抛
`UnicodeEncodeError` 导致初始化崩溃。**启动前必须设置 UTF-8 环境变量**：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
openviking-server
```

期望输出：

```
OpenViking HTTP Server is running on 127.0.0.1:1933
```

### 3.3 配置 JiuwenSwarm 外部记忆

JiuwenSwarm 配置文件路径：`~/.jiuwenswarm/config/config.yaml`。
在 `memory.external` 段启用 OpenViking（保持与现有配置一致，仅需确认
`provider` 为 `openviking` 且 `openviking.endpoint` 正确）：

```yaml
memory:
  mode: ${MEMORY_MODE:-local}
  engine: ${MEMORY_ENGINE:-builtin}
  forbidden_memory_definition:
    enabled: false
    patterns: []
    description:
      zh: "以下内容禁止记忆：（用户自行补充，如身高、体重、姓名等，建议用逗号分隔）"
      en: "The following content is forbidden to remember: (To be supplemented by users, e.g., height, weight, name, etc. Separate items with commas recommended)"
  external:
    provider: ${MEMORY_EXTERNAL_PROVIDER:-openviking}
    user_id: ${MEMORY_USER_ID:-__default__}
    scope_id: ${MEMORY_SCOPE_ID:-__default__}
    openjiuwen:
      kv_type: shelve
      kv_path: ${MEMORY_KV_PATH:-}
      vector_type: chroma
      vector_persist_dir: ${MEMORY_VECTOR_DIR:-}
      db_type: sqlite
      db_path: ${MEMORY_DB_PATH:-}
    mem0:
      api_key: ${MEM0_API_KEY:-}
      user_id: ${MEM0_USER_ID:-jiuwenswarm-user}
      agent_id: ${MEM0_AGENT_ID:-jiuwenswarm}
      rerank: true
    openviking:
      endpoint: ${OPENVIKING_ENDPOINT:-http://127.0.0.1:1933}
      api_key: ${OPENVIKING_API_KEY:-}
      account: ${OPENVIKING_ACCOUNT:-root}
      user: ${OPENVIKING_USER:-default}
    lakebase:
      api_key: ${LAKEBASE_API_KEY:-}
      base_url: ${LAKEBASE_API_URL:-}
      base_id: ${LAKEBASE_MEM_BASE_ID:-}
      database_id: ${LAKEBASE_DATABASE_ID:-}
```

> **本地方式**：`endpoint` 使用默认 `http://127.0.0.1:1933`，`api_key` 留空（dev 模式）。
> **联网方式**：通过环境变量覆盖：
>
> | 变量                                     | 说明                                              |
> | ---------------------------------------- | ------------------------------------------------- |
> | `OPENVIKING_ENDPOINT`                    | 远端服务地址，如 `https://openviking.example.com` |
> | `OPENVIKING_API_KEY`                     | 服务端创建的 User API Key                         |
> | `OPENVIKING_ACCOUNT` / `OPENVIKING_USER` | 对应账号与用户标识                                |
>
> 其余 `memory` 段配置保持与默认配置一致，无需修改。

### 3.4 数据目录迁移（可选，推荐）

为避免占用 C 盘，可将数据目录迁移至 D 盘并在 C 盘原位置建立 junction：

```bash
# 预演
python <skill>/scripts/setup_data_dir.py --dry-run --json
# 执行（目标目录默认 D:\OpenVikingData，可 --target 自定义）
python <skill>/scripts/setup_data_dir.py --yes --json
```

执行前需先停止 OpenViking 服务。

### 3.5 验证

```bash
# 服务健康
curl http://127.0.0.1:1933/health
# {"status":"ok","healthy":true,"version":"0.4.19","auth_mode":"dev"}

# 模型连通性（服务 + embedding + VLM 三项）
python <skill>/scripts/health_check.py --json
# {"server": {"ok": true, ...}, "embedding": {"ok": true, ...},
#  "vlm": {"ok": true, ...}, "all_ok": true}
```

---

## 4. 文档入库与检索

### 4.1 上传文档

OpenViking HTTP 服务端仅接受远程 URL 或 temp-uploaded 文件，不支持直接以
本地文件路径创建资源。需分两步：

```bash
# ① 上传本地文件，获取 temp_file_id
curl.exe -X POST "http://127.0.0.1:1933/api/v1/resources/temp_upload" \
  -F "file=@<本地文件绝对路径>"

# ② 使用 temp_file_id + source_name 创建资源
# POST /api/v1/resources
# body: {"temp_file_id": "<上一步返回的id>", "source_name": "<文件名>"}
```

### 4.2 语义检索

```bash
# POST /api/v1/search/find
# body: {"query": "<问题>", "target_uri": "viking://resources/<目录>", "limit": 5}
```

### 4.3 重索引（VLM 开通后补生成概览）

```bash
ov reindex "viking://resources/<目录>" --mode semantic_and_vectors --wait true
```

---

## 5. 注意事项

| 类别     | 注意事项                                                                                                  |
| -------- | --------------------------------------------------------------------------------------------------------- |
| 编码     | Windows 下必须设置 `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` 再启动，否则 rich 输出崩溃                   |
| 模型开通 | Embedding 与 VLM 模型须先在火山方舟开通；VLM 未开通会导致概览生成失败及服务连锁崩溃                       |
| 上传     | 本地文件必须先 `temp_upload` 再以 `temp_file_id + source_name` 创建资源；直接用 `path` 传本地路径返回 403 |
| 数据目录 | 迁移数据前先停止服务；C 盘 junction 建立后 `storage.workspace` 会同步更新                                 |
| 密钥     | API Key 通过配置工具/交互写入配置文件，避免手填进命令行；回显脱敏；文档示例一律使用占位符                 |
| CLI      | 首次使用 CLI 需 `ov language zh-CN`，并创建 `~/.openviking/ovcli.conf`                                    |
| 鉴权     | 联网方式务必使用绑定用户的 User/Admin Key 访问数据面，避免使用 Root Key                                   |

---

## 6. 参考

- [OpenViking 官方文档](https://docs.openviking.ai/)
- [OpenViking GitHub 仓库](https://github.com/volcengine/OpenViking)
- [记忆系统文档](./记忆.md)

---

## 返回导航

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)
