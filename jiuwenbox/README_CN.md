# jiuwenbox

`jiuwenbox` 是一个轻量级 Linux 沙箱服务，用于在分层隔离环境中运行
agent 工具和代码片段。

它提供一个 FastAPI 服务，用于管理沙箱生命周期、文件传输、文件
列表/搜索以及命令执行。服务在自身进程内直接调用 `bubblewrap` 启动
沙箱进程（每个沙箱一个长寿命 daemon，再加上按需的后台命令），由
预先解析的策略决定隔离细节。

## 功能特性

- 基于 `bubblewrap` 的进程隔离
- 基于静态 policy 的文件系统访问控制
- 服务端管理的沙箱后端存储（`~/.jiuwenbox/workspace`）
- 可选的 Linux 网络命名空间和防火墙网络隔离
- 命名空间和 Linux capability 控制
- 在内核支持时启用 Landlock 文件系统约束
- Seccomp 系统调用过滤
- 在运行时存在时支持 Python 和 JavaScript 代码执行
- 审计日志和持久化的沙箱生命周期状态
- 推理隐私代理，用于 LLM API 请求路由和自动 API 密钥注入

## 架构

- `server`
  - FastAPI 应用，负责沙箱生命周期管理、policy 加载、审计日志和 API 路由。
- `server/runtime`
  - 进程内运行时适配层，将沙箱 policy 翻译成 `bubblewrap` 命令行后由
    server 进程直接 spawn（每个沙箱一个长寿命 daemon，再加上按需的
    后台命令）。
- `server/proxy_manager`
  - 管理推理隐私代理，用于 LLM API 路由和 API 密钥注入。
- `server/policy_reader`
  - 共享 policy 文件读取器，供沙箱和代理管理器使用。
- `supervisor`
  - 策略到隔离的翻译辅助库（`bubblewrap` 命令构造、Landlock payload、
    seccomp 过滤器、cgroup/网络配置），供运行时适配层调用。
- `proxy`
  - HTTP 推理隐私代理，支持路径路由和 API 密钥注入（支持 OpenAI 和 Anthropic 格式）。
- `models`
  - 基于 Pydantic 的 policy、沙箱、API 响应和通用状态结构模型。

## 环境要求

- Linux
- Python 3.11+
- `bubblewrap`
- 使用 `network.mode: isolated` 时需要 `iproute2`、`iptables` 和 `nftables`
- `isolated` 模式启用 uplink 出网时，宿主机需开启 IPv4 转发（`net.ipv4.ip_forward=1`），进程需具备 `NET_ADMIN` capability
- 启用 Landlock 和 seccomp 时需要内核支持对应能力
- 如果需要执行 JavaScript，则需要 `nodejs`

Ubuntu 安装示例：

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap iproute2 iptables nftables python3-pip python3-venv nodejs
```

## 从源码安装

```bash
cd jiuwenswarm/jiuwenbox
uv venv
source .venv/bin/activate
uv sync
uv pip install --upgrade pip build
python3 -m build --wheel
uv pip install ./dist/jiuwenbox*.whl
```

构建出的 wheel 已包含 `jiuwenbox/configs/*.yaml`（源码位于 `src/jiuwenbox/configs/`）。
安装后若不设置 `JIUWENBOX_POLICY_PATH`，服务自动使用包内自带的 `default-policy.yaml`。

## 启动服务

### 本地启动

安装 wheel 后可直接启动（使用包内默认 policy）：

```bash
sudo ./.venv/bin/jiuwenbox-server
# 或
sudo ./.venv/bin/python -m uvicorn jiuwenbox.server.app:app --host 0.0.0.0 --port 8321 --log-level debug
```

如需指定其它 policy 或端口，设置 `JIUWENBOX_POLICY_PATH` 为**绝对路径**（也可在开发树里用 `src/jiuwenbox/configs/<name>.yaml`）：

```bash
sudo env \
  JIUWENBOX_POLICY_PATH="/absolute/path/to/policy.yaml" \
  ./.venv/bin/python -m uvicorn jiuwenbox.server.app:app --host 0.0.0.0 --port 9000 --log-level debug
```

### Docker 启动

构建镜像：

```bash
cd jiuwenswarm/jiuwenbox/scripts
sudo ./build_docker.sh
```

使用默认 policy 运行：

```bash
sudo ./run_docker.sh
```

### 通过 Unix Domain Socket 部署

jiuwenbox 支持把管理 HTTP API 跑在 Unix Domain Socket 上（与 TCP 二选一），
适用于同主机 agent 进程访问、需要文件系统权限控制访问者、或想避开
loopback 端口冲突的场景。上层协议仍是 HTTP/1.1，路由 / 请求体 / 响应都
与 TCP 模式完全一致。

监听地址由统一的环境变量 `JIUWENBOX_LISTEN` 控制，取以下两种形式之一：

```bash
JIUWENBOX_LISTEN=http://0.0.0.0:8321               # 默认
JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock  # 切到 UDS, 路径必须绝对
```

本地启动 UDS server（同上节 ⚠️ 的两条规则：`sudo env` 注 env、`./.venv/bin/`
绝对路径）：

```bash
sudo env \
  JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock \
  ./.venv/bin/python -m jiuwenbox.server.launcher

# 或直接用 uv sync / pip install 装好的入口脚本:
sudo env JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock \
  ./.venv/bin/jiuwenbox-server
```

Docker 部署 UDS：

```bash
mkdir -p /tmp/jiuwenbox-sock

sudo env \
  JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock \
  JIUWENBOX_UDS_HOST_DIR=/tmp/jiuwenbox-sock \
  ./run_docker.sh src/jiuwenbox/configs/default-policy.yaml
```

`run_docker.sh` 在 UDS 模式下会自动跳过管理 API 的 TCP 端口映射、把宿主
socket 目录挂进容器；**代理端口 `${JIUWENBOX_PROXY_PORT:-8322}` 仍按 TCP
映射**——Inference Privacy Proxy 是独立 TCP listener，与管理 API 传输无关。

接入示例：

```bash
# curl
curl --unix-socket /tmp/jiuwenbox-sock/jiuwenbox.sock http://localhost/health

# jiuwenbox CLI
jiuwenbox --base-url unix:///tmp/jiuwenbox-sock/jiuwenbox.sock health
JIUWENBOX_URL=unix:///tmp/jiuwenbox-sock/jiuwenbox.sock jiuwenbox sandbox ls

# pytest 双通路 (操作者先各自起好对应的 server)
pytest tests/integration --server-endpoint=http://127.0.0.1:8321
pytest tests/integration --server-endpoint=unix:///tmp/jiuwenbox-sock/jiuwenbox.sock
```

UDS 相关环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `JIUWENBOX_LISTEN` | `http://0.0.0.0:8321` | 管理 API 监听 URI；接受 `http://host:port` 或 `unix:///abs/socket/path`。 |
| `JIUWENBOX_UDS_MODE` | `0666` | UDS socket 文件权限 (八进制字符串)。Docker 场景下宿主与容器内 uvicorn uid 通常不同，默认放开；多租户 / 强隔离场景建议显式 `JIUWENBOX_UDS_MODE=0660` 并 `docker run --user $(id -u):$(id -g)` 收紧。 |
| `JIUWENBOX_UDS_HOST_DIR` | `/tmp/jiuwenbox-sock` | `run_docker.sh` 把宿主 socket 目录挂载到容器内的位置。 |
| `JIUWENBOX_UDS_CONTAINER_DIR` | `/run/jiuwenbox` | 容器内挂载点，必须与 `JIUWENBOX_LISTEN` 里 socket 路径所在的目录一致。 |

### API 认证（opt-in）

设置 `JIUWENBOX_API_TOKEN` 后，所有 HTTP 端点（含 `/health`、`/api/v1/*`、`/mcp`）均要求请求头：

```http
Authorization: Bearer <token>
```

未携带或 token 错误时返回 `401`，响应体 `{"error":"unauthorized"}`。

```bash
# 生成 token 并启动服务端
export JIUWENBOX_API_TOKEN="$(openssl rand -hex 32)"
JIUWENBOX_API_TOKEN=$JIUWENBOX_API_TOKEN jiuwenbox-server
# 或: jiuwenbox-server --api-token "$JIUWENBOX_API_TOKEN"

# CLI
export JIUWENBOX_API_TOKEN=$JIUWENBOX_API_TOKEN
jiuwenbox health

# curl
curl -H "Authorization: Bearer $JIUWENBOX_API_TOKEN" http://127.0.0.1:8321/health
```

| 变量 / 参数 | 说明 |
| --- | --- |
| `JIUWENBOX_API_TOKEN` | 服务端与 CLI **共用**；未设置 = 不启用认证 |
| `jiuwenbox-server --api-token` | 启动参数，优先级高于 env |
| `jiuwenbox --api-token` | CLI 参数，优先级高于 env |

### 持久化审计日志（`--save-logs DIR`）

**默认情况下 jiuwenbox 不会写任何日志文件**：审计事件只在 Python 标
准 logger 的 `DEBUG` 级别出现，沙箱 daemon 与后台 exec 的 stdout/stderr
直接送到 `/dev/null`，`/api/v1/sandboxes/{id}/logs` 返回空字符串。这样
保证一台新装的机器不会在 `$HOME` 下悄悄留下任何文件，也不会因为长期
运行的服务把磁盘写满。

传 `--save-logs DIR`（或环境变量 `JIUWENBOX_SAVE_LOGS_DIR=DIR`）即可
开启**审计日志**的持久化。文件**销毁沙箱时不再删除**，便于事后离线
分析、滚动归档、外挂到日志收集系统。

审计 JSONL 里**每个操作只落一行**，在调用返回后写出，同时携带"做了什么"
和"结果如何"。只看 JSONL 就能回答"这条指令到底成不成功"：

| event_type | 关键字段 |
| --- | --- |
| `exec_command` | `command`, `workdir`, `background?`, `ok`, `exit_code`, `stdout`, `stderr`, `duration_ms`, `error?`（stdout/stderr 默认尾部截断到 4 KiB，超出会标 `[truncated, total N chars]`；后台 exec 时改记 `started/pid` 而不是 `exit_code/stdout/stderr`） |
| `file_transfer` | `direction` (upload/download), `sandbox_path`, `size`, `ok`, `duration_ms`, `path`（`ipc` 还是 `exec_fallback`）, `error?` |

文件命名固定为 `{sandbox_id}-{ISO8601基本时间戳}.audit.log`，时间戳在
该 sandbox 第一次产生事件时确定并复用：

```
<DIR>/
  └── 9284a4bf-870-20260515T112345.audit.log   # 结构化 JSONL
```

ISO 8601 基本格式 (`%Y%m%dT%H%M%S`) 是为了让 `ls` 自然按时间排序；前缀
都是 sandbox_id，所以 `ls 9284a4bf-870-*` 能一次性看到一个沙箱所有
重启的审计文件。

本地启动：

```bash
sudo ./.venv/bin/jiuwenbox-server --save-logs /var/log/jiuwenbox

# 或走环境变量, 等价:
sudo env \
  JIUWENBOX_SAVE_LOGS_DIR=/var/log/jiuwenbox \
  ./.venv/bin/jiuwenbox-server
```

Docker 部署：传 `--save-logs DIR`（或设环境变量
`JIUWENBOX_SAVE_LOGS_HOST_DIR=DIR`），`run_docker.sh` 会自动 bind-mount 到
容器内 `JIUWENBOX_SAVE_LOGS_CONTAINER_DIR`（默认 `/var/log/jiuwenbox`），
并把 `JIUWENBOX_SAVE_LOGS_DIR=<容器路径>` 注入给 launcher，无需改
`Dockerfile`。命令行参数与环境变量等价，两者同时存在时 CLI 参数优先：

```bash
# CLI 参数（推荐）
sudo ./run_docker.sh --save-logs /tmp/jiuwenbox-logs

# 等价的环境变量写法
sudo env JIUWENBOX_SAVE_LOGS_HOST_DIR=/tmp/jiuwenbox-logs ./run_docker.sh

ls /tmp/jiuwenbox-logs
# 9284a4bf-870-20260515T112345.audit.log
```

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `JIUWENBOX_SAVE_LOGS_DIR` | _未设置_ | 容器内 / 进程内的目标审计日志目录；未设置即**完全不写日志文件**（默认）。launcher 会把 `--save-logs` / 环境变量解析为绝对路径写回此变量。 |
| `JIUWENBOX_SAVE_LOGS_HOST_DIR` | _未设置_ | `run_docker.sh` 专用：宿主侧目录（`--save-logs DIR` 的环境变量形式），留空即不开启日志持久化。设置后会自动 `mkdir -p`、bind-mount 到容器，并设置 `JIUWENBOX_SAVE_LOGS_DIR`。 |
| `JIUWENBOX_SAVE_LOGS_CONTAINER_DIR` | `/var/log/jiuwenbox` | `run_docker.sh` 在容器内的挂载点。一般无需修改；若容器内有别的进程占了这个路径再覆盖。 |

## Policy 文件

服务启动时会加载一个静态默认 policy。当前不启用 policy 动态更新功能。

### 字段说明

#### 顶层字段

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `version` | `1` | Policy schema 版本，当前仅支持 `1`。 |
| `name` | `"default"` | 可读名称，供 policy API 展示。 |
| `environment` | `{}` | 注入到沙箱内每个进程的环境变量键值对。 |

#### `filesystem_policy`

| 字段 | 说明 |
| --- | --- |
| `directories` | 沙箱生命周期内由服务端创建并 bind 进沙箱的目录。条目可以是 `"/path"` 字符串，或 `{ path, permissions }` 对象（`permissions` 为八进制，如 `"0755"`）。 |
| `files` | 沙箱生命周期内由服务端创建并 bind 进沙箱的空文件。格式与 `directories` 类似，支持可选 `permissions`。 |
| `read_only` | 沙箱内可见路径的只读授权列表；本身不会挂载宿主机路径，需配合 `bind_mounts` / `directories` 让路径存在。 |
| `read_write` | 沙箱内可见路径的读写授权列表；需通过 `directories` 或 `bind_mounts` 让路径实际存在。 |
| `bind_mounts` | 显式宿主机到沙箱的 bind mount，每项包含 `host_path`、`sandbox_path`、`mode`（`ro` / `rw`）。`host_path` 不能为字面量 `"*"`。 |
| `bind_root_entries` | 将 `host_root` 下**第一层**子项逐个 bind 到 `sandbox_path/{name}`。支持 `mode`、`include_hidden`（默认排除 `.` 开头项）、`exclude`（fnmatch glob）。适合批量挂载 `/usr` 等目录的直接子项。 |
| `device` | 通过 `bwrap --dev-bind` 暴露到沙箱内的设备节点，每项包含 `host_path` 和 `sandbox_path`。 |

#### `process`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `run_as_user` | `sandbox` | 沙箱内进程运行的用户名；无法解析时回退到 nobody 类 UID。 |
| `run_as_group` | `sandbox` | 沙箱内进程运行的组名；无法解析时回退到 nobody 类 GID。 |

#### `namespace`

控制 `bubblewrap` 创建的 Linux 命名空间，每项为 `true`（新建）或 `false`（复用当前）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `user` | `true` | 用户命名空间。 |
| `pid` | `true` | PID 命名空间。 |
| `ipc` | `true` | IPC 命名空间。 |
| `cgroup` | `true` | cgroup 命名空间。 |
| `uts` | `true` | UTS（主机名）命名空间。 |

#### `capabilities`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `add` | `[]` | 额外授予的 capability，如 `["CAP_NET_RAW"]` 或 `["NET_RAW"]`。 |
| `drop` | `[]` | 移除的 capability；`"ALL"` 表示在 bubblewrap 支持时丢弃全部 capability。 |

#### `landlock`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `compatibility` | `best_effort` | `disabled`：不启用 Landlock；`best_effort`：支持则启用，否则继续；`hard_requirement`：不支持则沙箱启动失败。 |

#### `syscall`

按 CPU 架构配置 seccomp 拦截的系统调用名列表：

| 字段 | 说明 |
| --- | --- |
| `x86_64.blocked` | x86_64 上拦截的 syscall 名，如 `ptrace`、`mount`、`bpf` 等。留空表示不额外拦截。 |
| `arm64.blocked` | arm64/aarch64 上拦截的 syscall 名。 |

#### `network`

出站（`egress`）和入站（`ingress`）流量规则。**仅在 `mode: isolated` 时生效**；`host` 模式下这些规则不会安装到沙箱内。

**`mode`**

| 模式 | 行为 |
| --- | --- |
| `isolated`（默认） | 为每个沙箱创建独立网络命名空间（`jbx-{sandbox_id}`），通过 veth uplink 连接宿主机默认路由，并在沙箱 netns 内安装 `egress` / `ingress` iptables 规则。 |
| `host` | 沙箱进程共享宿主机网络命名空间。**不会**在沙箱内安装 egress/ingress 防火墙规则，`blocked_ips`、`blocked_domains` 等字段不生效。host 模式仅通过 `uid-owner` iptables 保护 jiuwenbox 管理端口（默认 8321），防止沙箱进程访问管理 API。 |

内网部署若需要 egress 封禁（例如 `blocked_ips` 封禁 RFC1918 地址），应使用 `isolated` 模式并配置 `uplink`，不要依赖 `host` 模式。

**`uplink`**（仅 `isolated` 模式）

每个沙箱创建一个 veth 对（`jwbH{hash}` / `jwbS{hash}`），配置地址与默认路由，并可选通过宿主机 NAT 出网。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `subnet` | `""`（自动选择） | uplink 使用的 IPv4 地址池。留空时 jiuwenbox 依次扫描私有网段（`100.64.0.0/10`、聚焦的 `10.200.x/16` 段、`172.30.0.0/16`、`172.31.0.0/16`、`192.168.240.0/20`，以及 `10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`），在池内分配不与现有 IPv4 路由（`ip route show table all`）冲突的 `/30` 网段。显式配置时将该 CIDR 视为地址池，并在其中选取空闲 `/30`。 |
| `nat` | `true` | 是否在宿主机侧对 uplink 流量做 MASQUERADE。内网部署通常需要开启，沙箱才能通过宿主机默认路由访问外网。 |
| `interface` | `""`（自动探测） | 出网网卡名称。留空时自动探测宿主机默认路由对应的网卡。 |

**`egress` / `ingress`**

| 字段 | 说明 |
| --- | --- |
| `default` | `allow`：默认放行，仅 `blocked_*` 匹配项被拒绝；`deny`：默认拒绝，仅 `allowed_*` 匹配项被放行。 |
| `blocked_ips` / `allowed_ips` | CIDR 格式 IP 规则，作用于沙箱 netns 的 OUTPUT / INPUT 链。`blocked_*` 优先于 `allowed_*`。 |
| `blocked_domains` / `allowed_domains` | 域名规则，通过 DNS 解析后应用到解析出的 IP。 |
| `blocked_ports` / `allowed_ports` | TCP 端口规则。 |

iptables 规则写在沙箱 netns（`jbx-{sandbox_id}`）内，不在容器或宿主机的默认 netns。排查 egress 封禁时请进入对应 netns 查看：

```bash
ip netns list
ip netns exec jbx-<sandbox_id> iptables -L OUTPUT -n
```

`isolated` + uplink 的 Docker 部署需要 `net.ipv4.ip_forward=1` 和 `NET_ADMIN`；`run_docker.sh` 已默认配置。自行 `docker run` 时需授予同等权限。

#### `cgroup`

可选的每沙箱 cgroup 资源限制。三个字段默认均为 `null`（不限）；全部为空或省略 `cgroup` 块时，jiuwenbox 跳过 cgroup 设置，便于在无可用 cgroup 树的宿主机上运行。

| 字段 | 格式 | 说明 |
| --- | --- | --- |
| `memory_max` | 字节整数或带单位字符串（如 `"256M"`、`"1G"`） | 内存上限。 |
| `cpu_max` | 小数核数（如 `0.5`）或 `"quota_us period_us"` 对（如 `"50000 100000"`） | CPU 配额。 |
| `pids_max` | 正整数 | 进程/线程数上限。 |

优先使用 cgroup v2；v2 不可写时回退 v1。至少一个字段非空且两种后端均不可写时，沙箱创建失败。

#### `timeout`

jiuwenbox **服务端**的空闲沙箱淘汰配置，**仅在 server 启动时加载的根 policy 上生效**；per-sandbox policy 上的同名字段不影响沙箱隔离，仅用于配置回显。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `idle_timeout` | `null`（禁用） | 沙箱最大空闲时长（秒）。`null` / `0` / 负值表示禁用。空闲 = 自最后一次 exec / 文件 IO / 列目录等 API 调用以来的时长；`get_sandbox` / `list_sandboxes` / `get_logs` 不会刷新计时器。 |
| `idle_check_interval` | `60` | reaper 轮询间隔（秒），必须 `> 0`。 |

#### `inference_privacy_proxies`

推理隐私代理配置。`listen_port: 0`（默认）表示禁用；启用时需同时设置 `listen_host`（IP 地址）和 `listen_port > 0`。`routes` 定义按 `path_prefix` 转发的目标端点和 API 密钥注入规则。详见下文 [推理隐私代理](#推理隐私代理) 章节。

若 policy 仅包含 `version` / `name` / `inference_privacy_proxies` 且 `listen_port > 0`，jiuwenbox 会进入仅代理模式（跳过沙箱子系统）。参考 [`src/jiuwenbox/configs/inference-policy.yaml`](src/jiuwenbox/configs/inference-policy.yaml)。

### 最小示例

```yaml
version: 1
name: "example"

filesystem_policy:
  directories:
    - path: "/tmp"
      permissions: "1777"
  read_only:
    - "/bin"
    - "/sbin"
    - "/usr"
    - "/lib"
    - "/lib64"
    - "/etc"
  read_write:
    - "/tmp"
  bind_mounts:
    - host_path: "/bin"
      sandbox_path: "/bin"
      mode: "ro"
    - host_path: "/sbin"
      sandbox_path: "/sbin"
      mode: "ro"
    - host_path: "/usr"
      sandbox_path: "/usr"
      mode: "ro"
    - host_path: "/lib"
      sandbox_path: "/lib"
      mode: "ro"
    - host_path: "/lib64"
      sandbox_path: "/lib64"
      mode: "ro"
    - host_path: "/etc/resolv.conf"
      sandbox_path: "/etc/resolv.conf"
      mode: "ro"
    - host_path: "/etc/hosts"
      sandbox_path: "/etc/hosts"
      mode: "ro"
    - host_path: "/etc/nsswitch.conf"
      sandbox_path: "/etc/nsswitch.conf"
      mode: "ro"
    - host_path: "/etc/host.conf"
      sandbox_path: "/etc/host.conf"
      mode: "ro"
    - host_path: "/etc/ssl/certs"
      sandbox_path: "/etc/ssl/certs"
      mode: "ro"
    - host_path: "/etc/ssl/openssl.cnf"
      sandbox_path: "/etc/ssl/openssl.cnf"
      mode: "ro"
  device:
    - host_path: "/dev/null"
      sandbox_path: "/dev/null"

process:
  run_as_user: sandbox
  run_as_group: sandbox

namespace:
  user: true
  pid: true
  ipc: true
  cgroup: true
  uts: true

capabilities:
  add: []
  drop: []

landlock:
  compatibility: best_effort

syscall:
  x86_64:
    blocked:
      - "ptrace"
      - "mount"
      - "umount2"
      - "reboot"
      - "kexec_load"
  arm64:
    blocked:
      - "ptrace"
      - "mount"
      - "umount2"
      - "reboot"
      - "kexec_load"

network:
  mode: isolated
  uplink:
    nat: true
    interface: ""
  egress:
    default: allow
    allowed_domains: []
    blocked_domains: []
    allowed_ips:
      - "127.0.0.1/32"
      - "::1/128"
    blocked_ips: []
    allowed_ports:
      - 443
      - 80
    blocked_ports:
      - 22
  ingress:
    default: deny
    allowed_domains: []
    blocked_domains: []
    allowed_ips:
      - "127.0.0.1/32"
      - "::1/128"
    blocked_ips: []
    allowed_ports: []
    blocked_ports:
      - 22
```

## 在 jiuwenswarm 中通过配置文件启用 jiuwenbox

jiuwenswarm 通过 `config.yaml` 的 `sandbox` 段决定**是否启用沙箱、连接哪台 jiuwenbox、是否自己拉起 jiuwenbox 子进程、用哪个 policy**。一般用 TUI 的 `/sandbox` 命令操作时会自动落盘到这里，但也可以提前在 `config.yaml` 里手写。

### 配置 schema 与字段

```yaml
sandbox:
  # —— 端点 & 类型 ——
  url: "http://127.0.0.1:8321"      # jiuwenbox HTTP 端点；TCP 用 http://，UDS 用 unix:///abs/socket/path
  type: "jiuwenbox"                 # sandbox provider 名；当前固定为 jiuwenbox

  # —— 启动方式 & policy ——
  startup_mode: "internal"          # internal=agent-server 自动拉起 jiuwenbox-server；external=用户自行启动
  policy_file: "code-agent-policy.yaml"   # 仅文件名 → jiuwenbox/configs/<name>；含 / 或绝对路径 → 整路径
  preserve_file_sharing_mode: "mount"     # 仅支持 mount；写入其它值会被服务端拒绝

  # —— 运行时（也可由 /sandbox 命令维护） ——
  enabled: true                     # 是否处于沙箱模式
  excluded_commands:                # shell glob，命中后绕过沙箱在本地执行
    - "git *"
  fallback_on_failure: false        # jiuwenbox exec 异常时回退本地（非零 exit 不回退）
  files:                            # 用户配置的写入策略（auto-managed 路径不需要写在这里，服务端会自动注入）
    allow: []
    deny: []
```

字段说明：

| 字段 | 取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `sandbox.url` | URL 字符串 | `http://127.0.0.1:8321` | jiuwenbox 管理 API 端点。TCP 用 `http://host:port`；UDS 用 `unix:///abs/socket/path`（与 `JIUWENBOX_LISTEN` 配置的形态一致） |
| `sandbox.type` | 字符串 | `jiuwenbox` | sandbox provider 名。当前 jiuwenswarm 只接通了 `jiuwenbox` |
| `sandbox.startup_mode` | `internal` / `external` | `internal` | `internal`：agent-server 启动时自动 spawn `jiuwenbox-server` 子进程并落盘最终生效的 `url`（端口被占用时自动换端口）；`external`：jiuwenswarm 完全不碰 jiuwenbox 进程，要求按本 README 顶部的方式提前自己启动 |
| `sandbox.policy_file` | 文件名 / 路径 | `code-agent-policy.yaml` | 仅给文件名 → 自动定位到 `jiuwenbox/configs/<name>`；包含 `/` `\` 或 `~` 时按整路径解析。**仅在 `startup_mode=internal` 下生效**——`external` 模式下 policy 由用户自启动时的 `JIUWENBOX_DEFAULT_POLICY_PATH` 决定 |
| `sandbox.preserve_file_sharing_mode` | `mount` | `mount` | intrinsic 文件（`AGENT.md` 等）与 `project_dir` 通过 bind mount 注入沙箱，`project_dir/config/config.yaml` 自动加进 `deny_write`。 写入其它值会被服务端拒绝 |
| `sandbox.enabled` | bool | `false` | 启用后 agent 在重建时会切到 sandbox provider；可用 `/sandbox enable` 触发 |
| `sandbox.excluded_commands` | list[str] | `[]` | shell glob 列表；按 **simple-command 叶子**匹配。全命中→整条本地；全未命中→整条沙箱；混合→本地 bash 编排并用 `jiuwenbox sandbox exec` 包装远端段（需安装 CLI）。不安全的混合形态不改写，整条进沙箱 |
| `sandbox.fallback_on_failure` | bool | `false` | jiuwenbox exec 异常（连接失败、daemon 不可用等）时回退宿主机本地执行；沙箱内命令非零 exit 不回退 |
| `sandbox.files.allow` / `sandbox.files.deny` | list | `[]` | 用户额外配置的写入策略；最终生效集合是 `auto_managed ∪ user_configured`，详见 [`/sandbox` 命令说明](../docs/zh/Slash命令表.md) |

### 两种典型部署方式

#### 方式 A: `startup_mode: internal`（agent-server 帮你拉起 jiuwenbox）

适合本机开发 / 单机部署。直接在 `config.yaml` 里加：

```yaml
sandbox:
  url: "http://127.0.0.1:8321"
  type: "jiuwenbox"
  startup_mode: "internal"
  policy_file: "code-agent-policy.yaml"   # 用 jiuwenbox/configs/ 下的 policy
  enabled: true
```

agent-server 启动时会：

1. 把 `policy_file` 解析为宿主机绝对路径（仅文件名→`jiuwenbox/configs/<name>`；其它路径直接展开 `~` / `$VAR`）。
2. 探测 `url` 里的端口是否可用；冲突就自动换端口，并把最终的 `url` 写回 `config.yaml`，TUI `/sandbox status` 看到的就是真实端口。
3. spawn `jiuwenbox-server`，把 policy 路径传进去；启动失败会写一份 stderr 末尾到日志，TUI 仍能用 `/sandbox enable` 重试。

#### 方式 B: `startup_mode: external`（你自己启动 jiuwenbox-server）

适合需要把 jiuwenbox 跑在独立机器、容器里，或者 jiuwenswarm 进程不便用 root 的场景。

```yaml
sandbox:
  url: "http://10.0.0.5:8321"   # 或 unix:///run/jiuwenbox/jiuwenbox.sock
  type: "jiuwenbox"
  startup_mode: "external"
  enabled: true
```

此模式下 agent-server **不会**尝试拉起 jiuwenbox，`sandbox.policy_file` 也**不生效**（policy 由你启动 jiuwenbox-server 时通过 `JIUWENBOX_DEFAULT_POLICY_PATH` 指定）。jiuwenbox-server 的启动方式见前文 [`启动服务`](#启动服务) 与 [`通过 Unix Domain Socket 部署`](#通过-unix-domain-socket-部署)。

跨机部署要求 jiuwenbox 主机能访问 jiuwenswarm 的固有 agent 文件路径——`preserve_file_sharing_mode` 现在只支持 `mount` jiuwenswarm 会把 intrinsic 文件（`AGENT.md` / `HEARTBEAT.md` / `IDENTITY.md` / `SOUL.md` / `USER.md` / `memory/daily_memory/`）和 `project_dir` 通过 bind mount 暴露给沙箱，因此目标主机必须能在同样的 host path 下看到这些文件（例如共享文件系统、容器 volume 等）。

## 远程 MCP

JiuwenBox 支持三种访问方式：**REST API**、**CLI** 和**远程 MCP**。

MCP 端点路径为 `/mcp`（Streamable HTTP 传输）。当前 MCP 工具为
`sandbox_run_command`，用于在 JiuwenBox 沙箱内执行命令并返回结果。

### 快速启动

```bash
JIUWENBOX_POLICY_PATH=/path/to/default-policy.yaml \
python -m uvicorn jiuwenbox.server.app:app --host 0.0.0.0 --port 8321 --log-level debug
```

### OpenCode 配置

```json
{
  "mcpServers": {
    "jiuwenbox": {
      "url": "http://YOUR_HOST:8321/mcp",
      "type": "remote",
      "enabled": true
    }
  }
}
```

### 外部 IP 部署

当 JiuwenBox 部署在外部 IP（非 `localhost` / `127.0.0.1`）时，
需设置 `JIUWENBOX_MCP_ALLOWED_HOSTS` 以允许客户端主机访问：

```bash
JIUWENBOX_MCP_ALLOWED_HOSTS=10.0.0.5,10.0.0.5:8321 \
JIUWENBOX_POLICY_PATH=/path/to/default-policy.yaml \
python -m uvicorn jiuwenbox.server.app:app --host 0.0.0.0 --port 8321 --log-level debug
```

### 注意事项

- 直接 `GET /mcp` 可能返回 **406 Not Acceptable**，因为该端点期望
  MCP Streamable HTTP 协议帧。请使用正确的 MCP 客户端进行交互。
- MCP 使客户端能够调用 JiuwenBox，但**不代表**所有命令都必须经过
  沙箱。客户端自行决定哪些命令通过 MCP 路由。

## 推理隐私代理

推理隐私代理用于在边缘服务器上安全访问 LLM API：

- 路径路由到不同 LLM 提供商（OpenAI、Anthropic、自定义）
- 自动 API 密钥注入（OpenAI `Authorization: Bearer`、Anthropic `X-Api-Key`）
- 通过 REST API 热插拔（创建/启动/停止/重启/更新/删除）
- 通过 policy YAML 配置或REST API 管理

**架构说明**：

服务端运行一个全局代理进程，监听单一 host:port。

**隐私路由默认 `listen_port=0`（禁用）**，启用时需同时配置 `listen_host`（IP 地址）和 `listen_port`。

通过 `path_prefix`区分路由（转发规则）。**每条路由有独立状态**（`running` = 启用转发流量；`stopped` = 禁用）。

**通过 API 创建路由需 `listen_host` 有效且 `listen_port > 0`**，否则返回错误。

### 仅代理模式（proxy-only）

如果 policy YAML 文件**只配置 `inference_privacy_proxies`**（顶层 key 仅允许 `version` / `name` / `inference_privacy_proxies`，且 `listen_port > 0`），jiuwenbox 启动时会自动进入仅代理模式：

- 跳过沙箱子系统的初始化（不创建 `ProcessRuntime`、不注册 zombie reaper、不启动 idle reaper）。
- `GET /health` 仍可用，`sandboxes_active` 固定为 `0`。
- 沙箱相关路由（`/api/v1/sandboxes/*`、`/api/v1/policy/*`）返回 `503 Service Unavailable`，提示需要在 policy 里补充沙箱配置才能启用。
- `/api/v1/proxy/*` 路由及推理代理本身正常工作。

启动日志会打印 `Proxy-only policy detected (no sandbox config); skipping sandbox subsystem startup`，随后再输出 `Inference privacy proxy listening on http://<host>:<port>`，便于运维快速确认监听地址。

参考配置：[`src/jiuwenbox/configs/inference-policy.yaml`](src/jiuwenbox/configs/inference-policy.yaml)（安装后位于包内 `jiuwenbox/configs/`）。

### 代理配置

配置文件yaml文件说明：

```yaml
inference_privacy_proxies:
  listen_host: ipaddress，绑定的 IP 地址  # 必须
  listen_port: number：监听端口号         # 必须，非 0 值启用代理

  # 选填，可在启动后通过RESTAPI管理
  routes:
   - path_prefix: str，转发规则的路径名称
      target_endpoint: URL，目标端点
      api_key: str，转发时用于替换的api key
      skip_cert_verify: boolean，仅当target_endpoint为https且证书为自签名时跳过证书校验，调试用
```

### URL 路由

将
http://\<listening_host\>:\<listening_port\>/\<path_prefix\>/\<api_path\>
转发至
\<target_endpoint\>/\<api_path\>

### API 密钥注入

- OpenAI:     将 `Authorization: Bearer <placeholder>` 替换为实际密钥
- Anthropic: 将 `X-Api-Key: <placeholder>` 替换为实际密钥

### 配置示例

`注意：以下网络端点地址 https://api.openai.com、http://192.168.1.100:9000 均为示例`

#### 配置文件yaml示例

```yaml
inference_privacy_proxies:

  listen_host: "127.0.0.1"
  listen_port: 8080
  
  routes:
    - path_prefix: "openai"
      target_endpoint: "https://api.openai.com"
      api_key: "sk_sandbox_managed_openai_key"
   - path_prefix: "custom"
      target_endpoint: "http://192.168.1.100:9000"
      api_key: "sk_sandbox_managed_custom_key"
```

边缘服务器可使用 `listen_host: "0.0.0.0"` 接收所有网络接口的连接。

#### 转发示例

```text
客户端请求:  POST http://127.0.0.1:8322/openai/v1/chat/completions -H "Authorization: Bearer sk_fake_key"
代理转发:    POST https://api.openai.com/v1/chat/completions       -H "Authorization: Bearer sk_sandbox_managed_openai_key"

客户端请求:  POST http://127.0.0.1:8322/custom/v1/chat/completions -H "Authorization: Bearer sk_fake_key"
代理转发:    POST http://192.168.1.100:9000/v1/chat/completions    -H "Authorization: Bearer sk_sandbox_managed_custom_key"
```

#### jiuwenswarm 配置示例


| 配置项    | 旧值                          | 新值                             |
| --------- | ----------------------------- | -------------------------------- |
| api\_base | http://192.168.1.100:9000/v1/ | http://127.0.0.1:8322/custom/v1/ |
| api\_key  | sk_sandbox_managed_custom_key | sk_fake_key                      |

## 运行集成测试

`./tests/test.sh default` 会一次跑 `test_server_api_default.py` 和
`test_cli_default.py`，覆盖 server HTTP API 与 jiuwenbox CLI。通过
`--server-endpoint=URI` 切换连接方式，**传输协议自动从 URI 形式推断**：

```bash
# TCP（默认通路，等价于 --server-endpoint=http://127.0.0.1:8321；
# server 应以 default-policy.yaml 作为安全策略启动）
./tests/test.sh default

# 自定义 TCP 监听 (host:port 会自动补 http:// 前缀)
./tests/test.sh default --server-endpoint=http://127.0.0.1:18321
./tests/test.sh default --server-endpoint=127.0.0.1:18321

# UDS 通路: 直接给 socket 文件的绝对路径
./tests/test.sh default --server-endpoint=unix:///tmp/jiuwenbox.sock
./tests/test.sh default --server-endpoint=unix:///tmp/jiuwenbox-sock/jiuwenbox.sock
```

注意 test.sh 本身**不会**起 server，请按选定通路先手工启动对应的 jiuwenbox
(TCP 走 `JIUWENBOX_LISTEN=http://0.0.0.0:8321` 或自定义端口，UDS 走
`JIUWENBOX_LISTEN=unix:///...`)。

运行指定测试用例：

```bash
python3 -m pytest tests/integration/test_server_api_default.py::TestPolicyEnforcement::test_network_mode_isolated_allows_external_http_requests -s --server-endpoint 127.0.0.1:8321
python3 -m pytest tests/integration/test_server_api_default.py::TestPolicyEnforcement::test_network_mode_isolated_blocked_ip_rejects_egress -s --server-endpoint 127.0.0.1:8321
```

### MCP 集成测试

`test_mcp_default.py` 针对 `/mcp` Streamable HTTP 端点和 `sandbox_run_command`
MCP 工具进行集成测试。执行 `./tests/test.sh default` 时会自动包含该文件，也可以单独运行：

```bash
# TCP
python3 -m pytest tests/integration/test_mcp_default.py -v --server-endpoint 127.0.0.1:8321

# UDS
python3 -m pytest tests/integration/test_mcp_default.py -v --server-endpoint=unix:///tmp/jiuwenbox.sock
```

MCP 测试会建立真实的 MCP 客户端会话，在沙箱中执行命令，并验证沙箱自动创建 /
复用 / 删除、stdin / env / workdir 透传、命令失败透传、timeout 截断以及并发会话等边界行为。

### 性能测试

运行日常办公 workload 性能测试：

```bash
./tests/test.sh performance --server-endpoint 127.0.0.1:8321
```

可通过脚本参数设置沙箱数量、每个沙箱内的并发数，以及每个任务的循环次数：

```bash
./tests/test.sh performance \
  --sandbox-count 2 \
  --concurrency 16 \
  --loop 8 \
  --server-endpoint 127.0.0.1:8321
```

脚本会把这些参数映射为性能测试 fixture 使用的环境变量：

| 脚本参数 | 环境变量 | 默认值 |
| -------- | -------- | ------ |
| `--sandbox-count` | `JIUWENBOX_PERF_SANDBOX_COUNT` | `1` |
| `--concurrency` | `JIUWENBOX_PERF_CONCURRENCY` | `4` |
| `--loop` | `JIUWENBOX_PERF_LOOP` | `8` |

### 真实 LLM 集成测试

运行真实 LLM 集成测试需设置以下环境变量，若未设置环境变量，这些测试默认跳过：

```bash
export JIUWENBOX_TEST_LLM_ENDPOINT="https://api.openai.com"
export JIUWENBOX_TEST_LLM_API_KEY="sk_sandbox_managed_key"
export JIUWENBOX_TEST_LLM_MODEL="YOUR_MODEL"
```

## 注意事项

- 修改启动 policy 文件后，需要重启服务。
- 已存在的沙箱会继续使用创建时写入的 policy。
- `/exec` API 会把命令 stderr 作为命令执行结果返回；如果服务端诊断日志
  可能污染命令 stderr，应使用 debug 级别日志。

## CLI

`jiuwenbox` 提供单文件 Python CLI 客户端，包装
[`docs/jiuwenbox_server_api.md`](docs/jiuwenbox_server_api.md) 中所有 HTTP 接口。

`pip install` 之后会安装 `jiuwenbox` 可执行命令；源码内运行用
`python -m jiuwenbox.cli.jiuwenbox`。

```bash
# 健康检查
jiuwenbox health

# 沙箱生命周期
ID=$(jiuwenbox sandbox create | jq -r .id)
jiuwenbox sandbox exec "$ID" -- python3 -c 'print("hi")'
JOB=$(jiuwenbox sandbox bg-exec "$ID" --job-id http-srv -- python3 -m http.server 18080 | jq -r .job_id)
jiuwenbox sandbox bg-get "$ID" "$JOB"
jiuwenbox sandbox bg-list "$ID"
jiuwenbox sandbox bg-kill "$ID" "$JOB"
jiuwenbox sandbox upload "$ID" ./data.csv /tmp/data.csv
jiuwenbox sandbox download "$ID" /tmp/result.json - | jq .
jiuwenbox sandbox ls
jiuwenbox sandbox rm "$ID" --yes

# 沙箱策略
jiuwenbox policy get "$ID"

# 代理管理
jiuwenbox proxy create --prefix /openai --target https://api.openai.com --api-key sk-xxx
jiuwenbox proxy logs openai --lines 50
```

全局选项：

| 选项 | 默认值 | 环境变量 | 说明 |
| --- | --- | --- | --- |
| `--base-url` | `http://127.0.0.1:8321` | `JIUWENBOX_URL` | jiuwenbox 服务地址。接受 `http://host:port` 或 `unix:///abs/socket/path` |
| `--api-token` | _未设置_ | `JIUWENBOX_API_TOKEN` | Bearer token；服务端启用认证时必须与服务端 token 一致 |
| `--timeout` | `30` | `JIUWENBOX_TIMEOUT` | HTTP 超时秒数 |
| `--verbose / -v` | 关闭 | – | stderr 打印 debug 日志 |
| `--no-color` | 关闭 | `NO_COLOR` | 关闭 stderr ANSI 颜色 |

退出码：`0` 成功 / `sandbox exec` 沙箱内退出码为 0；`1` HTTP 4xx/5xx；`2`
连接失败；`3` 本地参数 / 文件错误，或 `sandbox bg-get` / `sandbox bg-kill`
遇到 job 不存在（404）；`130` Ctrl+C。`sandbox exec` 子命令
会透传沙箱内进程的退出码；`sandbox bg-exec` 在 `started=false` 时返回 `3`。

## License

Apache-2.0
