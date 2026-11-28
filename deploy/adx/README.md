# JiuwenSwarm 分布式一键部署方案

AgentOS 多用户使用 develop 的运行标识、数据面热同步、Conch 与 Code Graph 配置见
[AgentOS 适配说明](AGENTOS_ADAPTATION.md)。

## 简介

本目录使用已部署的 ADX 管理 AgentServer，保留 JiuwenSwarm 的安装、Gateway/Web 启停、`--hosts` 和 SSH/SCP 流程。脚本不安装 ADX 集群，也不注册函数。

## 前置要求

- **ADX 已正常安装部署**：HTTP Ingress 可达，租户凭据、AgentServer 镜像和运行时已准备。
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
| `CLUSTER_HOSTS`     | 目标主机 IP 列表，逗号分隔。第一个 IP 为 Gateway/Web 主机，其余为应用安装主机。也可通过命令行 `--hosts` 参数指定，命令行优先级更高。不设置时默认使用本机 IP | `""`   |
| `PYTHON_VERSION` | Python 版本，用于远程安装 jiuwenswarm 依赖（当前仅支持3.11）                                                       | `3.11` |

#### jiuwenswarm 配置

| 变量                        | 说明                                                                                                           | 默认值  |
| ------------------------- | ------------------------------------------------------------------------------------------------------------ | ---- |
| `JIUWENSWARM_PACKAGE_URL` | jiuwenswarm 安装包 URL（远程主机未安装 jiuwenswarm 时使用此 URL 进行 pip 安装）。如果远程主机已安装 jiuwenswarm，此配置不需要设置                   | `""` |

#### ADX 连接配置

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `ADX_INGRESS_URL` | 已部署的 ADX HTTPS Ingress origin，例如 `https://adx.example:8443` | 必填 |
| `ADX_TENANT` | 租户名 | `agentos` |
| `ADX_TOKEN` | 对应租户的访问凭据 | 必填 |
| `ADX_AGENTSERVER_IMAGE` | 已在 ADX 准备好的 AgentServer 镜像标识 | `agentos-supervisor:v1` |
| `ADX_AGENTSERVER_RUNTIME` | AgentServer 运行时 | `supervisor` |

原 `FUNC_SVC_NAME`、`FUNCTION_ID`、`YR_FUNC_CODE_DIR`、`MGR_*` 和
`SANDBOX_TYPE` 属于已移除的函数注册配置；`GATEWAY_CONCURRENCY` 和
`GATEWAY_INVOKE_TIMEOUT` 属于旧函数调用协议。AgentServer 的资源仍由
`AGENTOS_BUILTIN_AGENT_CPU` / `AGENTOS_BUILTIN_AGENT_MEMORY` 和管理面控制，
不应把旧函数资源直接映射到 AgentServer。安装器的旧 `YR_PYTHON_VERSION`
会作为 `PYTHON_VERSION` 的兼容输入；显式新值优先。

#### Gateway 配置

| 变量                          | 说明                                                                                                 | 默认值                 |
| --------------------------- | -------------------------------------------------------------------------------------------------- | ------------------- |
| `JIUWENSWARM_INSTANCE_NAME` | jiuwenswarm 实例名。留空时使用 `~/.jiuwenswarm`，指定后使用 `~/.jiuwenswarm-instances/<实例名>`。 | `""` |
| `JIUWENSWARM_GATEWAY_CONFIG_DIR` | Gateway 配置目录覆盖。绝对路径直接使用，相对路径按 `<workspace>/infra/config/<override>` 解析。留空时使用 `<workspace>/infra/config`。 | `""` |
| `GATEWAY_SESSION_MAP_SCOPE` | SessionMap 会话映射策略。可选值：`per_chat_bot`（同聊天窗口+机器人共享会话）、`per_chat_bot_user`（按用户维度隔离会话）                 | `per_chat_bot_user` |
| `EXTENSION_DIRS`            | 扩展包搜索目录（远程主机上的路径，多个目录用分号 `;` 分隔）。留空则自动从 jiuwenswarm 安装路径推断：`<pip_location>/jiuwenswarm/extensions` | `""`                |

#### Gateway 独立部署配置

以下配置用于 Gateway/Web 的部署主机选择。ADX 地址和凭据来自上面的连接配置，不从历史函数 ID 或 Frontend 地址推断。

| 变量               | 说明                                              | 默认值  |
| ---------------- | ----------------------------------------------- | ---- |
| `MASTER_NODE_IP` | Master 节点 IP（单独部署 gateway 时需要指定）                | `""` |

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
# 部署所有模块（jiuwenswarm + gateway + web），默认本机
./deploy.sh up

# 指定主机部署所有模块
./deploy.sh up --hosts 192.168.1.1

# 仅部署 jiuwenswarm 模块（安装 jiuwenswarm）
./deploy.sh up jiuwenswarm --hosts 192.168.1.1

# 仅部署 gateway 模块（需先安装 jiuwenswarm，并配置 ADX 连接参数）
./deploy.sh up gateway --hosts 192.168.1.1

# 多机部署（第一个 IP 为 Gateway/Web 主机，其余为应用安装主机）
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

### 刷新单个用户配置

`refresh-user-config.sh` 用于把某个逻辑用户的配置刷新为 root 模板，同时保留该用户的
`models.agentos`。

实际实现为同目录的 `refresh_user_config.py`，`refresh-user-config.sh` 只是入口包装。

```
# 预览将要执行的动作（不写文件、不发请求）
./refresh-user-config.sh user_68 --dry-run

# 刷新该用户配置
./refresh-user-config.sh user_68 -y

# 批量：刷新所有已初始化用户
./refresh-user-config.sh --all -y
```

可选参数：`--home-base`（默认 `/home/agentos/users`）、`--template`
（默认 `/root/.jiuwenswarm/config/config.yaml`；命名实例为
`/root/.jiuwenswarm-instances/<name>/config/config.yaml`）、`--all`。
脚本必须以 root 运行，目标用户需先由 `agentos.sh create-user` 初始化。
该脚本刷新 AS 的 `config/config.yaml`。Gateway 使用独立的 `infra/config/config.yaml`。

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
./deploy.sh up --hosts 192.168.1.1       # 部署所有模块（jiuwenswarm + gateway + web）
./deploy.sh down --hosts 192.168.1.1     # 卸载所有模块
./deploy.sh restart --hosts 192.168.1.1  # 重启所有模块
```

### 模块列表（选填）

部署工具支持对以下三个独立模块进行精细化管理：

- `jiuwenswarm`：在所有 host 上安装 jiuwenswarm
- `gateway`：jiuwenswarm Gateway 服务（进程模式）
- `web`：静态 Web 服务，代理业务请求到 Gateway

单模块操作示例：

```
./deploy.sh [操作命令] jiuwenswarm --hosts 192.168.1.1   # 仅操作 jiuwenswarm 模块
./deploy.sh [操作命令] gateway --hosts 192.168.1.1       # 仅操作 gateway 模块
```

当未指定模块参数时，默认按 `jiuwenswarm → gateway → web` 顺序依次部署三个模块。

### 配置参数（选填）

- `--hosts`：指定集群主机 IP 列表，逗号分隔。第一个 IP 为 Gateway/Web 主机，其余为应用安装主机。也可在 `.env.custom` 中通过 `CLUSTER_HOSTS` 配置，命令行参数优先级更高。不指定时默认使用本机 IP
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

- **前置条件**：先部署 ADX，再安装应用和启动 Gateway/Web。
- **jiuwenswarm 模块**：通过原有本机/SSH 安装流程在指定 hosts 安装应用，不注册函数。
- **gateway 模块**：需配置可达的 ADX Ingress 和有效租户凭据。
- **部署顺序**：同时部署时按 `jiuwenswarm → gateway → web` 启动，停止时逆序。
- **down jiuwenswarm**：不卸载 pip 包，也不停止 ADX 集群；包卸载由 AgentOS 的 uninstall 负责。

## Gateway 实例配置

通过 `.env.custom` 中的 `JIUWENSWARM_INSTANCE_NAME` 可配置 gateway 实例名：

- 未设置实例名（默认）：
  - AS：`~/.jiuwenswarm/config/config.yaml`
  - Gateway：`~/.jiuwenswarm/infra/config/config.yaml`
- 设置实例名（如 `JIUWENSWARM_INSTANCE_NAME=prod`）：
  - AS：`~/.jiuwenswarm-instances/prod/config/config.yaml`
  - Gateway：`~/.jiuwenswarm-instances/prod/infra/config/config.yaml`

实例名配置后，`jiuwenswarm-init` 和 `jiuwenswarm-gateway` 命令会自动通过 `JIUWENSWARM_DATA_DIR` 环境变量指向对应的实例目录。

AgentOS 升级时，`gateway_handler.sh` 在 `jiuwenswarm-init` 前调用
`python -m jiuwenswarm.agentos_migration`。迁移优先读取旧 Gateway 配置
`infra/config/config.yaml`，也支持旧版一体机的共享 `config/config.yaml`。
用户配置迁入 `infra/config/config.yaml`，VIP、etcd 和 ADX 连接等字段采用本次部署值。
旧 AS 配置由不带 `-f` 的 `jiuwenswarm-init` 保留并补齐新键。

## AgentOS 发布包升级

发布包使用 `SWARM_SETTINGS` 加载 OS 的 `agentos_handler.sh`。
Enterprise 在启动服务前运行 `configure.py configure`，从部署 settings 和凭据文件生成
Gateway 的 ADX YAML / service dotenv，迁移已有用户 YAML/.env，并准备新用户模板。
旧用户无需自行新增 ADX 地址或 token；保留业务模型、有效工具资源、身份和挂载配置。

独立运行本目录脚本时，没有 Enterprise 的部署 settings 与凭据文件，需在 `.env.custom`
显式提供 ADX 连接参数。历史函数 ID/Frontend 地址不能替代 ADX 的租户凭据。
两种入口均沿用 `up/down/restart`；目录名从 `deploy/yuanrong` 改为 `deploy/adx`，
AgentOS Server 内安装路径仍为 `deploy/jiuwenswarm/`。
