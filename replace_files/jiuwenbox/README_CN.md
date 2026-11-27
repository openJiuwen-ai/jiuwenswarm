# jiuwenbox

`jiuwenbox` 是一个轻量级 Linux 沙箱服务，用于在分层隔离环境中运行
agent 工具和代码片段。

它提供一个 FastAPI 服务，用于管理沙箱生命周期、文件传输、文件
列表/搜索以及命令执行。每个沙箱命令都会通过一个小型 supervisor
进程启动，由 supervisor 根据配置好的隔离策略应用沙箱限制。

## 功能特性

- 基于 `bubblewrap` 的进程隔离
- 基于静态 policy 的文件系统访问控制
- 通过 `sandbox_workspace` 配置沙箱后端工作目录
- 可选的 Linux 网络命名空间和防火墙网络隔离
- 命名空间和 Linux capability 控制
- 在内核支持时启用 Landlock 文件系统约束
- Seccomp 系统调用过滤
- 在运行时存在时支持 Python 和 JavaScript 代码执行
- 审计日志和持久化的沙箱生命周期状态

## 架构

- `server`
  - FastAPI 应用，负责沙箱生命周期管理、policy 加载、审计日志和 API 路由。
- `server/runtime`
  - 运行时适配层，负责为每个沙箱命令启动一个 supervisor 进程。
- `supervisor`
  - 每条命令的启动器，负责将生效的 policy 转换为 `bubblewrap`、Landlock、
    seccomp 和命名空间配置。
- `models`
  - 基于 Pydantic 的 policy、沙箱、API 响应和通用状态结构模型。

## 目录结构

```text
configs/
  default-policy.yaml
  jiuwenclaw-policy.yaml
docker/
scripts/
  server.sh
  test.sh
src/jiuwenbox/
  models/
  server/
  supervisor/
tests/
  integration/
```

## 环境要求

- Linux
- Python 3.11+
- `bubblewrap`
- 使用 `network.mode: isolated` 时需要 `iproute2` 和 `iptables`
- 启用 Landlock 和 seccomp 时需要内核支持对应能力
- 如果需要执行 JavaScript，则需要 `nodejs`

Ubuntu 安装示例：

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap iproute2 iptables python3-pip nodejs
```

## 安装

```bash
cd jiuwenclaw/jiuwenbox
uv venv
source .venv/bin/activate
uv sync
```

## 启动服务

### 本地启动

`scripts/server.sh` 将第一个位置参数作为 policy 文件路径。该路径会按照
调用脚本时所在的当前目录进行解析，然后转换为绝对路径传给服务。

```bash
sudo ./scripts/server.sh
sudo ./scripts/server.sh configs/default-policy.yaml
sudo ./scripts/server.sh configs/jiuwenclaw-policy.yaml 9000
```

默认端口是 `8321`。第二个位置参数如果是数字，则会作为端口号使用。
其余参数会继续透传给 `uvicorn`。

选中的 policy 路径会导出为：

```bash
JIUWENBOX_DEFAULT_POLICY_PATH=/absolute/path/to/policy.yaml
```

端口也可以通过环境变量设置：

```bash
JIUWENBOX_PORT=9000 sudo ./scripts/server.sh configs/default-policy.yaml
```

### Docker 启动

构建镜像：

```bash
cd jiuwenclaw/jiuwenbox/docker
sudo ./build_docker.sh
```

使用默认 policy 运行：

```bash
sudo ./run_docker.sh
```

Docker 镜像默认会启动 `./scripts/server.sh`。

## Policy 文件

服务启动时会加载一个静态默认 policy。当前不启用 policy 动态更新功能。

重要字段：

- `sandbox_workspace`
  - 用于服务端管理沙箱后端存储的宿主机目录。
  - 该值在展开 `~` 和环境变量之后必须是绝对路径。
- `filesystem_policy.directories`
  - 由服务端创建并在沙箱生命周期内绑定到沙箱中的目录。
- `filesystem_policy.read_only`
  - 沙箱内授予只读访问权限的路径；这些条目本身不会挂载 host 路径。
- `filesystem_policy.read_write`
  - 沙箱内授予读写访问权限的路径；需要通过 `directories` 或 `bind_mounts`
    让这些路径实际存在于沙箱内。
- `filesystem_policy.bind_mounts`
  - 显式的宿主机到沙箱路径的 bind mount 配置。

路径字段支持 shell 风格的展开，例如 `~` 和环境变量。

最小示例：

```yaml
version: 1
name: "example"
sandbox_workspace: "/sandbox"

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
  blocked:
    - "ptrace"
    - "mount"
    - "umount2"
    - "reboot"
    - "kexec_load"

network:
  mode: isolated
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

## API 示例

创建沙箱：

```bash
curl -sS -X POST http://127.0.0.1:8321/api/v1/sandboxes \
  -H 'Content-Type: application/json' \
  -d '{"command":["/usr/bin/python3","-c","import time; time.sleep(3600)"]}'
```

执行命令：

```bash
curl -sS -X POST http://127.0.0.1:8321/api/v1/sandboxes/<sandbox-id>/exec \
  -H 'Content-Type: application/json' \
  -d '{"command":["/usr/bin/python3","-c","print(\"hello\")"],"workdir":"/tmp"}'
```

上传文件：

```bash
curl -sS -X POST \
  'http://127.0.0.1:8321/api/v1/sandboxes/<sandbox-id>/upload?sandbox_path=/tmp/input.txt' \
  -F 'file=@input.txt'
```

下载文件：

```bash
curl -sS \
  'http://127.0.0.1:8321/api/v1/sandboxes/<sandbox-id>/download?sandbox_path=/tmp/input.txt'
```

删除沙箱：

```bash
curl -sS -X DELETE http://127.0.0.1:8321/api/v1/sandboxes/<sandbox-id>
```

## 运行集成测试

```bash
./scripts/test.sh
```

运行指定 policy 对应的集成测试：

```bash
./scripts/test.sh default # jiuwenbox 使用 default-policy.yaml 作为安全策略运行服务。
./scripts/test.sh jiuwenclaw-tool # jiuwenbox 使用 jiuwenclaw-tool-policy.yaml 作为安全策略运行服务。
```

## 注意事项

- 修改启动 policy 文件后，需要重启服务。
- 已存在的沙箱会继续使用创建时写入的 policy。
- `/exec` API 会把命令 stderr 作为命令执行结果返回；如果服务端诊断日志
  可能污染命令 stderr，应使用 debug 级别日志。

## License

Apache-2.0
