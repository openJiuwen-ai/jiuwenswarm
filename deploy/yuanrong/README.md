# JiuwenSwarm 分布式一键部署方案

## 简介

openYuanrong 是一个 Serverless 分布式计算引擎，旨在为分布式应用提供高性能运行和集群资源的高效利用。基于此引擎，我们打造了 JiuwenSwarm 分布式一键部署方案。

基于 openYuanrong 进程部署模式，对 JiuwenSwarm 进行函数化部署，并以进程方式部署 gateway，无需 K8s 集群，适合快速体验和轻量级部署场景。

## 前置要求

- **openyuanrong 已正常安装部署**：执行本脚本前，需确保 openyuanrong 已在所有目标主机上安装并启动，集群处于正常运行状态。
- 操作系统：Linux
- 系统架构：amd64 或 arm64
- 硬件资源：单机最低配置为 16 核 CPU 及 32GB 内存
- 网络要求：部署机器到目标主机需配置 SSH 免密登录

您可以通过以下命令检查：

```
# 操作系统
uname -s
# 系统架构
uname -m
# CPU核心数
nproc
# 检查内存大小 (GB)
free -g | grep Mem | awk '{print $2}'
```

配置部署机器到所有目标主机的免密 SSH 登录：

```
ssh-copy-id root@<目标主机IP>
```

## 配置

参考部署目录下 [.env.example](.env.example) 配置模板，按需修改环境变量、运行模式等自定义参数，完成业务与环境适配。将配置文件复制为 `.env.custom` 后修改：

```
cp .env.example .env.custom
vim .env.custom
```

### 配置项说明

#### 集群主机配置

| 变量                  | 说明                                                                                               | 默认值    |
| ------------------- | ------------------------------------------------------------------------------------------------ | ------ |
| `CLUSTER_HOSTS`     | 目标主机 IP 列表，逗号分隔。第一个 IP 为 yr master 节点，其余为 agent 节点。也可通过命令行 `--hosts` 参数指定，命令行优先级更高。不设置时默认使用本机 IP | `""`   |
| `YR_PYTHON_VERSION` | Python 版本，用于远程安装 jiuwenswarm 依赖（当前仅支持3.11）                                                       | `3.11` |

#### jiuwenswarm 配置

| 变量                        | 说明                                                                                                           | 默认值  |
| ------------------------- | ------------------------------------------------------------------------------------------------------------ | ---- |
| `YR_FUNC_CODE_DIR`        | jiuwenswarm 函数代码目录（远程主机上的路径）。留空则自动从远程主机 pip 安装路径推断：`$(pip show jiuwenswarm Location)/jiuwenswarm/extensions` | `""` |
| `JIUWENSWARM_PACKAGE_URL` | jiuwenswarm 安装包 URL（远程主机未安装 jiuwenswarm 时使用此 URL 进行 pip 安装）。如果远程主机已安装 jiuwenswarm，此配置不需要设置                   | `""` |

#### 函数服务配置

| 变量                   | 说明                          | 默认值              |
| -------------------- | --------------------------- |------------------|
| `FUNC_SVC_NAME`      | 函数服务名称                      | `0@jiuwen@swarm` |
| `MGR_CPU`            | 单实例 CPU 限制（单位：毫核，300=0.3 核） | `300`            |
| `MGR_MEMORY`         | 单实例内存限制（单位：Mi）              | `600`            |
| `MGR_MIN_INSTANCE`   | 服务最小实例数                     | `1`              |
| `MGR_MAX_INSTANCE`   | 服务最大实例数                     | `10`             |
| `MGR_CONCURRENT_NUM` | 服务并发处理数量/最大并发数              | `10`             |

#### Gateway 配置

| 变量                          | 说明                                                                                                 | 默认值                 |
| --------------------------- | -------------------------------------------------------------------------------------------------- | ------------------- |
| `JIUWENSWARM_INSTANCE_NAME` | jiuwenswarm 实例名（留空则使用默认路径 `~/.jiuwenswarm`）。指定实例名后配置目录变为 `~/.jiuwenswarm-instances/<实例名>`          | `""`                |
| `GATEWAY_CONCURRENCY`       | 网关同时向后端发送的最大并发请求数                                                                                  | `1`                 |
| `GATEWAY_INVOKE_TIMEOUT`    | 网关调用后端接口的超时时间（秒）                                                                                   | `60`                |
| `GATEWAY_SESSION_MAP_SCOPE` | SessionMap 会话映射策略。可选值：`per_chat_bot`（同聊天窗口+机器人共享会话）、`per_chat_bot_user`（按用户维度隔离会话）                 | `per_chat_bot_user` |
| `EXTENSION_DIRS`            | 扩展包搜索目录（远程主机上的路径，多个目录用分号 `;` 分隔）。留空则自动从 jiuwenswarm 安装路径推断：`<pip_location>/jiuwenswarm/extensions` | `""`                |

#### Gateway 独立部署配置

以下配置仅在单独部署 gateway 时需要手动设置。如果先部署 jiuwenswarm 再部署 gateway，这些值会自动获取，无需手动设置。

| 变量               | 说明                                              | 默认值  |
| ---------------- | ----------------------------------------------- | ---- |
| `MASTER_NODE_IP` | Master 节点 IP（单独部署 gateway 时需要指定）                | `""` |
| `FRONTEND_PORT`  | 元戎 Frontend 端口（单独部署 gateway 时需要指定，默认 8888）      | `""` |
| `FUNCTION_ID`    | 函数 ID（单独部署 gateway 时需要指定，由 jiuwenswarm 注册函数后返回） | `""` |

#### 大模型接口配置

| 变量               | 说明                 | 默认值  |
| ---------------- | ------------------ | ---- |
| `MODEL_PROVIDER` | 模型厂商标识（如：OpenAI 等） | `""` |
| `MODEL_NAME`     | 大模型名称              | `""` |
| `API_BASE`       | 大模型 API 基础地址       | `""` |
| `API_KEY`        | 大模型鉴权密钥            | `""` |

#### 向量模型接口配置

| 变量               | 说明            | 默认值  |
| ---------------- | ------------- | ---- |
| `EMBED_MODEL`    | 向量模型名称        | `""` |
| `EMBED_API_BASE` | 向量模型 API 基础地址 | `""` |
| `EMBED_API_KEY`  | 向量模型鉴权密钥      | `""` |

## 使用方法

### 一键部署

```
# 部署所有模块（jiuwenswarm + gateway），默认本机
./deploy.sh up

# 指定主机部署所有模块
./deploy.sh up --hosts 192.168.1.1

# 仅部署 jiuwenswarm 模块（安装jiuwenswarm + 函数注册）
./deploy.sh up jiuwenswarm --hosts 192.168.1.1

# 仅部署 gateway 模块（需先部署 jiuwenswarm 或在 .env.custom 中配置 FUNCTION_ID 等参数）
./deploy.sh up gateway --hosts 192.168.1.1

# 多机部署（第一个IP为yr master，其余为agent）
./deploy.sh up --hosts 192.168.1.1,192.168.1.2,192.168.1.3
```

### 一键卸载

```
# 卸载所有模块
./deploy.sh down --hosts 192.168.1.1

# 仅卸载 gateway
./deploy.sh down gateway --hosts 192.168.1.1

# 仅卸载 jiuwenswarm
./deploy.sh down jiuwenswarm --hosts 192.168.1.1
```

### 一键重启

```
./deploy.sh restart --hosts 192.168.1.1
```

## 参数解析

命令格式：

```
./deploy.sh [操作命令(必填)] [模块列表(选填)] [配置参数(选填)]
```

### 操作命令（必填）

部署工具支持三种核心操作，用于管理服务生命周期：

- `up`：部署并启动指定的业务模块
- `down`：停止并卸载指定的业务模块
- `restart`：重启指定的业务模块

基础用法（无模块参数）：

```
./deploy.sh up --hosts 192.168.1.1       # 部署所有模块（jiuwenswarm + gateway）
./deploy.sh down --hosts 192.168.1.1     # 卸载所有模块
./deploy.sh restart --hosts 192.168.1.1  # 重启所有模块
```

### 模块列表（选填）

部署工具支持对以下两个独立模块进行精细化管理：

- `jiuwenswarm`：在所有 host 上安装 jiuwenswarm + 在 yr master 节点注册函数
- `gateway`：jiuwenswarm Gateway 服务（进程模式）

单模块操作示例：

```
./deploy.sh [操作命令] jiuwenswarm --hosts 192.168.1.1   # 仅操作 jiuwenswarm 模块
./deploy.sh [操作命令] gateway --hosts 192.168.1.1       # 仅操作 gateway 模块
```

当未指定模块参数时，默认按 `jiuwenswarm → gateway` 顺序依次部署两个模块。

### 配置参数（选填）

- `--hosts`：指定集群主机 IP 列表，逗号分隔。第一个 IP 为 yr master 节点，其余为 agent 节点。也可在 `.env.custom` 中通过 `CLUSTER_HOSTS` 配置，命令行参数优先级更高。不指定时默认使用本机 IP
- `-h, --help`：显示帮助信息

参数使用示例：

```
./deploy.sh up --hosts 192.168.1.1                          # 单机部署所有模块
./deploy.sh up --hosts 192.168.1.1,192.168.1.2,192.168.1.3  # 多机部署所有模块
./deploy.sh up jiuwenswarm --hosts 192.168.1.1              # 单机仅部署 jiuwenswarm
./deploy.sh down gateway --hosts 192.168.1.1                # 仅卸载 gateway
./deploy.sh up                                              # 默认本机部署所有模块
```

## 重要约束

- **前置条件**：执行 deploy.sh 前需确保 openyuanrong 已在所有目标主机上正常安装并启动
- **jiuwenswarm 模块**：在所有 host 上安装 jiuwenswarm，但函数注册只在 yr master 节点（第一个 IP）执行
- **gateway 模块**：依赖 jiuwenswarm 模块部署后产生的 `FUNCTION_ID`、`FRONTEND_PORT` 等参数。如果单独部署 gateway，需在 `.env.custom` 中手动配置这些参数
- **部署顺序**：同时部署时，jiuwenswarm 先于 gateway 部署；卸载时，gateway 先于 jiuwenswarm 卸载
- **down jiuwenswarm**：仅删除 yr master 上注册的函数元信息，不卸载 jiuwenswarm pip 包、不停止 openyuanrong 集群

## Gateway 实例配置

通过 `.env.custom` 中的 `JIUWENSWARM_INSTANCE_NAME` 可配置 gateway 实例名：

- 未设置实例名（默认）：gateway 使用默认路径 `~/.jiuwenswarm/config/config.yaml`
- 设置实例名（如 `JIUWENSWARM_INSTANCE_NAME=prod`）：gateway 使用实例路径 `~/.jiuwenswarm-instances/prod/config/config.yaml`

实例名配置后，`jiuwenswarm-init` 和 `jiuwenswarm-gateway` 命令会自动通过 `JIUWENSWARM_DATA_DIR` 环境变量指向对应的实例目录。
