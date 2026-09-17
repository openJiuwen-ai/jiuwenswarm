# jiuwenbox 服务接口说明

本文档基于当前 jiuwenbox 服务端源码整理，描述 HTTP 接口的用途、参数和响应格式。

## 基础信息

> **下文所有 `requests.get("http://127.0.0.1:8321/...")` 代码示例的 URL
> 路径不变**；切到 Unix Domain Socket 部署时只需把 base URL 替换成
> "本节通用接入示例" 中的 UDS client 即可。状态码、错误格式、所有请求 /
> 响应字段在两种传输方式下完全一致。

### TCP 监听（默认）

uvicorn 直接 bind `0.0.0.0:8321`，外部走 IP:Port 访问：

```text
http://127.0.0.1:8321
```

### Unix Domain Socket 监听

适用于同主机 agent 进程访问、或需要靠文件系统权限做访问控制的场景。
启动方式见仓库根的 [`README_CN.md`](../README_CN.md#通过-unix-domain-socket-部署)
"通过 Unix Domain Socket 部署" 一节；典型 endpoint：

```text
unix:///tmp/jiuwenbox-sock/jiuwenbox.sock     # 宿主机视角的 socket 文件
unix:///run/jiuwenbox/jiuwenbox.sock          # 容器内 uvicorn 绑定的路径
```

服务端单进程一次只绑一种 listener；并发提供 TCP + UDS 需要部署两份 jiuwenbox。

业务接口统一使用 `/api/v1` 前缀，健康检查接口为 `/health`。

### API 认证（opt-in）

设置环境变量 `JIUWENBOX_API_TOKEN`（或 `jiuwenbox-server --api-token`）后启用。
所有 HTTP 端点（含 `/health`、`/api/v1/*`、`/mcp`）均需在请求头携带：

```http
Authorization: Bearer <token>
```

未设置 `JIUWENBOX_API_TOKEN` 时行为与旧版一致（无认证）。错误 token 返回
`401`，响应体 `{"error":"unauthorized"}`。

服务端与 jiuwenbox CLI 共用 `JIUWENBOX_API_TOKEN`；CLI 亦支持 `--api-token`
（优先级高于 env）。jiuwenclaw / openjiuwen provider 当前不会自动发送该
header，启用认证后需自行在 HTTP 客户端层携带 token。

### 通用接入示例

以下三种方式分别通过 TCP 与 UDS 访问同一个 `/health`，请求行为完全等价；
本文后续小节里 `http://127.0.0.1:8321/...` 形式的示例只需照此替换 base URL。

curl：

```bash
# TCP
curl http://127.0.0.1:8321/health

# TCP 并设置认证 token
curl -H "Authorization: Bearer $JIUWENBOX_API_TOKEN" http://127.0.0.1:8321/health

# UDS
curl --unix-socket /tmp/jiuwenbox-sock/jiuwenbox.sock http://localhost/health
```

Python（`requests` 库本身不原生支持 UDS，UDS 路径推荐 `httpx` 或
[`requests-unixsocket`](https://pypi.org/project/requests-unixsocket/)）：

```python
# TCP
import requests
resp = requests.get("http://127.0.0.1:8321/health", timeout=30)

# UDS
import httpx
transport = httpx.HTTPTransport(uds="/tmp/jiuwenbox-sock/jiuwenbox.sock")
with httpx.Client(transport=transport, base_url="http://jiuwenbox", timeout=30) as client:
    resp = client.get("/health")
```

jiuwenbox CLI：

```bash
# TCP (默认)
jiuwenbox health

# UDS
jiuwenbox --base-url unix:///tmp/jiuwenbox-sock/jiuwenbox.sock health
JIUWENBOX_URL=unix:///tmp/jiuwenbox-sock/jiuwenbox.sock jiuwenbox health
```

### 请求与响应约定

- JSON 请求使用 `Content-Type: application/json`
- 文件上传使用 `multipart/form-data`
- 文件下载返回 `application/octet-stream`
- 日志接口通常返回 `text/plain`

成功响应通常直接返回 JSON 对象或数组，例如：

```json
{
  "id": "abc123",
  "phase": "ready"
}
```

删除、上传等无响应体接口成功时返回 `204 No Content`。

错误响应通常为：

```json
{
  "error": "Sandbox 'abc123' not found"
}
```

部分代理接口使用 FastAPI 默认错误格式：

```json
{
  "detail": "Proxy 'openai' not found"
}
```

### 常见状态码

| 状态码 | 含义 |
| --- | --- |
| `200` | 请求成功 |
| `201` | 资源创建成功 |
| `204` | 请求成功，无响应体 |
| `400` | 请求参数错误或 policy 校验失败 |
| `404` | 沙箱、策略、文件、目录或代理不存在 |
| `409` | 当前状态不允许执行该操作 |
| `500` | 服务端内部错误 |

## 通用数据结构

### 沙箱引用信息

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 沙箱 ID |
| `phase` | string | 沙箱状态，取值为 `provisioning`、`ready`、`stopped`、`error`、`deleting` |
| `runtime` | string | 当前固定为 `process` |
| `pid` | integer/null | 沙箱生命周期进程 PID |
| `created_at` | string | 创建时间 |
| `started_at` | string/null | 启动时间 |
| `error_message` | string/null | 错误信息 |
| `env` | object | 创建沙箱时注入的环境变量 |
| `ip_address` | string/null | 最近一次成功 create/start 后确认的 IPv4 快照。`network.mode=isolated` 时为沙箱 veth 地址；`host` 时为 jiuwenbox 与沙箱共享网络命名空间的出口 IPv4（若服务运行在 Docker/WSL 中，可能是容器或 WSL 地址，不承诺物理宿主机 LAN 地址）。`provisioning`、创建/启动失败的 `error` 为 `null`；`stopped` 保留停止前最后一次有效地址。get/list 不重新探测，只返回该快照 |

示例：

```json
{
  "id": "abc123def456",
  "phase": "ready",
  "runtime": "process",
  "pid": 12345,
  "created_at": "2026-04-25T11:30:00.000000",
  "started_at": "2026-04-25T11:30:01.000000+00:00",
  "error_message": null,
  "env": {
    "DEMO_KEY": "demo-value"
  },
  "ip_address": "100.64.12.34"
}
```

### 命令执行结果

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `exit_code` | integer | 命令退出码 |
| `stdout` | string | 标准输出 |
| `stderr` | string | 标准错误 |

### 后台执行结果

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `started` | boolean | 后台进程是否创建成功 |
| `pid` | integer/null | supervisor 进程 PID |
| `command` | string[] | 实际执行的命令 |
| `error_message` | string/null | 创建失败原因 |

### 健康检查结果

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | 固定为 `ok` |
| `version` | string | 服务版本 |
| `runtime` | string | 当前固定为 `process` |
| `landlock_supported` | boolean | 当前主机是否支持 Landlock |
| `sandboxes_active` | integer | 当前处于 `ready` 状态的沙箱数量 |

## 健康检查

### 健康检查接口

接口：`GET /health`

用途：检查服务是否存活，并返回运行时信息。

Python 请求示例：

```python
import requests

resp = requests.get("http://127.0.0.1:8321/health", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "status": "ok",
  "version": "0.1.0",
  "runtime": "process",
  "landlock_supported": true,
  "sandboxes_active": 1
}
```

## 沙箱接口

### 创建沙箱

接口：`POST /api/v1/sandboxes`

用途：创建沙箱。

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `env` | object | 否 | 沙箱公共环境变量 |
| `policy` | object/null | 否 | 覆盖或追加的 policy 数据 |
| `policy_mode` | string | 否 | `override` 或 `append`，默认 `override` |
| `sandbox_id` | string/null | 否 | 可选，指定沙箱 ID。长度 4~16，仅允许小写字母、数字、减号（`-`）和下划线（`_`）。省略或空字符串时服务端自动生成（形如 `6011f5ca-76a`）。格式非法返回 400；与已有 ID 冲突返回 409 |

Python 请求示例：

```python
import requests

resp = requests.post(
    "http://127.0.0.1:8321/api/v1/sandboxes",
    json={
        "sandbox_id": "my-sb_01",
        "env": {
            "DEMO_KEY": "demo-value"
        },
        "policy_mode": "override"
    },
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

响应示例（指定 `sandbox_id` 时返回该值；省略时自动生成，形如 `6011f5ca-76a`）：

```json
{
  "id": "my-sb_01",
  "phase": "ready",
  "runtime": "process",
  "pid": 12345,
  "created_at": "2026-04-25T11:30:00.000000",
  "started_at": "2026-04-25T11:30:01.000000+00:00",
  "error_message": null,
  "env": {
    "DEMO_KEY": "demo-value"
  },
  "ip_address": "100.64.12.34"
}
```

### 查询沙箱列表

接口：`GET /api/v1/sandboxes`

用途：列出全部沙箱。

Python 请求示例：

```python
import requests

resp = requests.get("http://127.0.0.1:8321/api/v1/sandboxes", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
[
  {
    "id": "abc123def456",
    "phase": "ready",
    "runtime": "process",
    "pid": 12345,
    "created_at": "2026-04-25T11:30:00.000000",
    "started_at": "2026-04-25T11:30:01.000000+00:00",
    "error_message": null,
    "env": {},
    "ip_address": "100.64.12.34"
  }
]
```

### 查询沙箱状态

接口：`GET /api/v1/sandboxes/{sandbox_id}`

用途：查询单个沙箱状态。

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.get(f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "id": "abc123def456",
  "phase": "ready",
  "runtime": "process",
  "pid": 12345,
  "created_at": "2026-04-25T11:30:00.000000",
  "started_at": "2026-04-25T11:30:01.000000+00:00",
  "error_message": null,
  "env": {},
  "ip_address": "100.64.12.34"
}
```

### 删除沙箱

接口：`DELETE /api/v1/sandboxes/{sandbox_id}`

用途：删除沙箱。

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.delete(f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}", timeout=30)
print(resp.status_code)
```

响应示例：

```text
204 No Content
```

### 启动沙箱

接口：`POST /api/v1/sandboxes/{sandbox_id}/start`

用途：启动已停止的沙箱。

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.post(f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}/start", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "id": "abc123def456",
  "phase": "ready",
  "runtime": "process",
  "pid": 12345,
  "created_at": "2026-04-25T11:30:00.000000",
  "started_at": "2026-04-25T11:31:00.000000+00:00",
  "error_message": null,
  "env": {},
  "ip_address": "100.64.12.34"
}
```

### 停止沙箱

接口：`POST /api/v1/sandboxes/{sandbox_id}/stop`

用途：停止沙箱。

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.post(f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}/stop", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "id": "abc123def456",
  "phase": "stopped",
  "runtime": "process",
  "pid": null,
  "created_at": "2026-04-25T11:30:00.000000",
  "started_at": "2026-04-25T11:31:00.000000+00:00",
  "error_message": null,
  "env": {},
  "ip_address": "100.64.12.34"
}
```

### 重启沙箱

接口：`POST /api/v1/sandboxes/{sandbox_id}/restart`

用途：重启沙箱。

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.post(f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}/restart", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "id": "abc123def456",
  "phase": "ready",
  "runtime": "process",
  "pid": 22345,
  "created_at": "2026-04-25T11:30:00.000000",
  "started_at": "2026-04-25T11:32:00.000000+00:00",
  "error_message": null,
  "env": {},
  "ip_address": "100.64.12.34"
}
```

### 同步执行命令

接口：`POST /api/v1/sandboxes/{sandbox_id}/exec`

用途：在沙箱内同步执行命令，等待命令结束后返回结果。

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `command` | string[] | 是 | 待执行命令 |
| `workdir` | string/null | 否 | 命令工作目录 |
| `env` | object/null | 否 | 本次执行追加环境变量 |
| `stdin` | string/null | 否 | 标准输入文本 |
| `timeout_seconds` | integer/null | 否 | 超时时间，单位秒 |

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.post(
    f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}/exec",
    json={
        "command": ["python3", "-c", "print('hello')"],
        "timeout_seconds": 10
    },
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "exit_code": 0,
  "stdout": "hello\n",
  "stderr": ""
}
```

### 启动后台命令

接口：`POST /api/v1/sandboxes/{sandbox_id}/exec_background`

用途：在沙箱内启动后台进程，进程创建后立即返回。与 ``exec`` 相同，命令由沙箱 daemon 在**同一 PID 命名空间**内 ``fork+exec``，不再为每次后台任务单独 spawn bubblewrap。

`running`、`exit_code` 为**返回时刻的快照**（daemon 在 spawn 后立即响应）。瞬时命令可能在 `exec_background` 响应里已显示 `running=false`；长任务则 `running=true`。要可靠判断结束并读取输出，请轮询 `GET .../background/{job_id}`。

`pid` 为**沙箱 PID 命名空间内**的用户进程 pid（与 ``sandbox exec ps`` 中看到的 pid 一致），不是 host 上的 bwrap 监控进程 pid。

请求体（`BackgroundExecRequest`）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `command` | string[] | 是 | 要执行的命令 argv |
| `job_id` | string | 否 | 自定义 job id（4–16 位，`[0-9a-z_-]`）；省略则服务端自动生成 |
| `workdir` | string | 否 | 沙箱内工作目录 |
| `env` | object | 否 | 额外环境变量 |
| `stdin` | string | 否 | 标准输入文本 |
| `timeout_seconds` | int | 否 | 预留字段 |

后台任务**不捕获** stdout/stderr（始终丢弃）；需要输出时请使用同步 `exec`。

错误码：`job_id` 格式非法 → **400**；同 sandbox 内 `job_id` 已占用 → **409**；沙箱非 ready → **409**。

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.post(
    f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}/exec_background",
    json={
        "job_id": "http-srv",
        "command": ["python3", "-m", "http.server", "18080"],
        "workdir": "/tmp",
    },
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

响应示例（`exec_background` 返回时刻快照，进程通常仍在运行）：

```json
{
  "started": true,
  "job_id": "http-srv",
  "pid": 23456,
  "command": ["python3", "-m", "http.server", "18080"],
  "running": true,
  "exit_code": null,
  "error_message": null
}
```

轮询 `GET .../background/{job_id}` 直至 `running=false` 后，可得到最终状态（示例：`python3 --version` 结束后）：

```json
{
  "job_id": "9284a4bf-870",
  "sandbox_id": "abc123def456",
  "command": ["python3", "--version"],
  "pid": 23457,
  "running": false,
  "exit_code": 0,
  "started_at": "2026-06-16T10:00:01.000000",
  "finished_at": "2026-06-16T10:00:01.050000",
  "stdout": "",
  "stderr": "",
  "workdir": null
}
```

### 查询后台任务

接口：`GET /api/v1/sandboxes/{sandbox_id}/background/{job_id}`

用途：查询单个后台任务状态。stdout/stderr 恒为空（后台任务不记录输出）。

响应体（`BackgroundJobStatus`）含：`job_id`、`sandbox_id`、`command`、`pid`、`running`、`exit_code`、`started_at`、`finished_at`、`stdout`、`stderr`、`workdir`。

沙箱或 job 不存在 → **404**。

### 列出后台任务

接口：`GET /api/v1/sandboxes/{sandbox_id}/background`

查询参数：

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `running_only` | bool | `false` | 为 `true` 时仅返回仍在运行的 job |

响应：`{"items": [BackgroundJobSummary, ...]}`，按 `started_at` 降序排列。

### 终止后台任务

接口：`POST /api/v1/sandboxes/{sandbox_id}/background/{job_id}/kill`

请求体（`KillBackgroundJobRequest`，可省略）：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `signal` | int | `15` | 发送的信号编号（如 `9` = SIGKILL） |

响应体（`KillBackgroundJobResult`）：`job_id`、`killed`、`reason`（`ok` / `already_exited` / `permission_denied`）、`exit_code`。

发信号后立即返回（非阻塞）；客户端可轮询 `GET .../background/{job_id}` 确认 `running=false`。

#### CLI 对应命令

```bash
# 启动后台任务
jiuwenbox sandbox bg-exec my-sb --job-id http-srv -- python3 -m http.server 18080

# 查询 / 列出 / 终止（stdout 均为 JSON）
jiuwenbox sandbox bg-get my-sb http-srv
jiuwenbox sandbox bg-list my-sb
jiuwenbox sandbox bg-list my-sb --running-only
jiuwenbox sandbox bg-kill my-sb http-srv
jiuwenbox sandbox bg-kill my-sb http-srv --signal 9
```

### 查看沙箱日志

接口：`GET /api/v1/sandboxes/{sandbox_id}/logs`

用途：读取沙箱审计日志。

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.get(f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}/logs", timeout=30)
print(resp.status_code)
print(resp.text)
```

响应示例：

```text
[2026-04-25T11:30:00.000000] sandbox_created
[2026-04-25T11:30:01.000000] sandbox_started
```

### 上传文件

接口：`POST /api/v1/sandboxes/{sandbox_id}/upload`

用途：向沙箱内上传文件。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `sandbox_path` | string | 是 | 沙箱内目标路径 |

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
with open("local.txt", "rb") as f:
    resp = requests.post(
        f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}/upload",
        params={"sandbox_path": "/tmp/remote.txt"},
        files={"file": ("local.txt", f)},
        timeout=30,
    )
print(resp.status_code)
```

响应示例：

```text
204 No Content
```

### 下载文件

接口：`GET /api/v1/sandboxes/{sandbox_id}/download`

用途：从沙箱内下载文件。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `sandbox_path` | string | 是 | 沙箱内源文件路径 |

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.get(
    f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}/download",
    params={"sandbox_path": "/tmp/remote.txt"},
    timeout=30,
)
print(resp.status_code)
print(resp.content)
```

响应示例：

```text
二进制文件内容
```

### 列出文件

接口：`GET /api/v1/sandboxes/{sandbox_id}/files`

用途：列出沙箱目录下的文件和目录。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `sandbox_path` | string | 是 | 待列举目录 |
| `recursive` | boolean | 否 | 是否递归 |
| `max_depth` | integer/null | 否 | 递归深度限制 |
| `include_files` | boolean | 否 | 是否包含文件，默认 `true` |
| `include_dirs` | boolean | 否 | 是否包含目录，默认 `true` |

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.get(
    f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}/files",
    params={
        "sandbox_path": "/tmp",
        "recursive": "true",
        "max_depth": 2,
        "include_files": "true",
        "include_dirs": "true",
    },
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "items": [
    {
      "name": "remote.txt",
      "path": "/tmp/remote.txt",
      "size": 12,
      "is_directory": false,
      "modified_time": "2026-04-25T11:35:00.000000",
      "type": ".txt"
    }
  ]
}
```

### 搜索文件

接口：`GET /api/v1/sandboxes/{sandbox_id}/search`

用途：按 glob 模式搜索文件。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `sandbox_path` | string | 是 | 搜索根目录 |
| `pattern` | string | 是 | 匹配模式 |
| `exclude_patterns` | string[] | 否 | 排除模式，可重复传入 |

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.get(
    f"http://127.0.0.1:8321/api/v1/sandboxes/{sandbox_id}/search",
    params={
        "sandbox_path": "/tmp",
        "pattern": "*.txt",
        "exclude_patterns": "*.bak",
    },
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "items": [
    {
      "name": "remote.txt",
      "path": "/tmp/remote.txt",
      "size": 12,
      "is_directory": false,
      "modified_time": "2026-04-25T11:35:00.000000",
      "type": ".txt"
    }
  ]
}
```

## Policy 接口

### 查询沙箱策略

接口：`GET /api/v1/policies/{sandbox_id}`

用途：获取某个沙箱当前生效的 policy。

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.get(f"http://127.0.0.1:8321/api/v1/policies/{sandbox_id}", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "name": "default-policy",
  "process": {
    "run_as_user": "nobody",
    "run_as_group": "nobody"
  },
  "network": {
    "mode": "host"
  }
}
```

### 更新单个沙箱网络策略

接口：`PUT /api/v1/policies/{sandbox_id}`

用途：动态更新指定沙箱的 `network.egress` / `network.ingress`。
请求体字段与创建沙箱一致（`policy` + `policy_mode`）。
本期仅支持改 ingress/egress；`network.mode` / `uplink` 以及其它 policy 段会返回 400。
对 `network.mode: isolated` 且正在运行的沙箱会热更新 netns 内 iptables；已停止的沙箱只落盘，下次 start 生效。`host` 模式返回 400。

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `policy` | object | 是 | 本期须含 `network`，且 `egress` / `ingress` 至少提供一个 |
| `policy_mode` | string | 否 | `override`（默认）或 `append`，语义与创建沙箱相同 |

Python 请求示例：

```python
import requests

sandbox_id = "abc123def456"
resp = requests.put(
    f"http://127.0.0.1:8321/api/v1/policies/{sandbox_id}",
    json={
        "policy_mode": "override",
        "policy": {
            "network": {
                "egress": {
                    "default": "allow",
                    "blocked_ips": ["198.51.100.10/32"],
                },
                "ingress": {
                    "default": "allow",
                    "blocked_ports": [8080],
                },
            },
        },
    },
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

成功时返回更新后的完整 `SecurityPolicy` JSON（与 `GET /policies/{sandbox_id}` 同形）。

### 批量更新所有沙箱网络策略

接口：`PUT /api/v1/policies`

用途：对当前所有已注册沙箱应用与单沙箱接口相同的网络规则更新。
不修改 server 默认 policy。请求体非法时整请求 400，不落任何沙箱。
`host` 模式沙箱记入 `skipped`；单个沙箱热更新失败记入 `failed`，不阻断其余沙箱。

请求体与 `PUT /api/v1/policies/{sandbox_id}` 相同。

Python 请求示例：

```python
import requests

resp = requests.put(
    "http://127.0.0.1:8321/api/v1/policies",
    json={
        "policy_mode": "append",
        "policy": {
            "network": {
                "egress": {
                    "blocked_ips": ["203.0.113.50/32"],
                },
            },
        },
    },
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "updated": ["sb-1", "sb-2"],
  "skipped": [
    {"sandbox_id": "sb-host", "reason": "network.mode is host"}
  ],
  "failed": []
}
```

### 查询 timeout 配置

接口：`GET /api/v1/timeout`

用途：返回服务级别的空闲沙箱回收 (idle reaper) 当前生效的配置，
对应 `policy.timeout`。`idle_timeout` 为 `null` 表示 reaper 已关闭。

Python 请求示例：

```python
import requests

resp = requests.get("http://127.0.0.1:8321/api/v1/timeout", timeout=30)
print(resp.json())
```

响应示例：

```json
{
  "idle_timeout": 1800,
  "idle_check_interval": 60
}
```

### 更新 timeout 配置

接口：`PUT /api/v1/timeout`

用途：原子更新 idle reaper 的一个或两个字段，并立即重启 reaper。
请求体里**仅显式出现的字段会被应用**，省略的字段保留当前值，
所以可以单独翻 `idle_timeout` 而不影响 `idle_check_interval`。

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `idle_timeout` | number 或 `null` | 空闲超时秒数。`null` 或 `<= 0` 关闭 reaper。可省略以保留当前值。 |
| `idle_check_interval` | number | reaper 轮询间隔秒数，必须 `> 0`；**不允许** `null`，省略字段以保留当前值。 |

Python 请求示例：

```python
import requests

resp = requests.put(
    "http://127.0.0.1:8321/api/v1/timeout",
    json={"idle_timeout": 1200},
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

响应示例（成功，返回更新后的完整 `TimeoutPolicy`）：

```json
{
  "idle_timeout": 1200,
  "idle_check_interval": 60
}
```

校验失败（如 `idle_check_interval <= 0`，或显式传入
`idle_check_interval: null`）返回 `400 Bad Request`，body 中包含
具体错误描述。

## 代理接口

代理接口用于管理 inference privacy proxy。

### 创建代理

接口：`POST /api/v1/proxies`

用途：创建一个代理路由。

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `path_prefix` | string | 是 | 路由前缀，如 `/openai` |
| `target_endpoint` | string | 是 | 目标服务地址 |
| `api_key` | string | 否 | 注入到上游的 API Key（Bearer / X-Api-Key）。与 `basic_auth` 互斥 |
| `basic_auth` | object | 否 | HTTP Basic 凭据。与 `api_key` 互斥。见下 |
| `skip_cert_verify` | boolean | 否 | 是否跳过证书校验 |

`basic_auth` 对象字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `username` | string | 是 | Basic 认证用户名，禁止 CR/LF/NUL 等控制字符 |
| `password` | string | 否 | 内联密码，**仅用于开发测试**（明文存储于策略/请求中）。与 `password_file` 二选一 |
| `password_file` | string | 否 | 服务端可读的密码文件路径（推荐 K8s Secret / Docker Secret / `0600` 文件）。与 `password` 二选一。读取时仅去除结尾换行 |

约束：`api_key` 与 `basic_auth` 互斥；`password` 与 `password_file` 必须二选一；`password_file` 必须存在、为普通文件且当前进程可读（`0640`/`0644` 不阻断，仅告警）；空文件或仅换行的文件会被拒绝（`password_file is empty`）。仅支持 HTTP Basic，不支持 Digest/OAuth/Neo4j Bolt。任何接口都不返回明文密码（仅返回 `username` / `password_configured` / `password_file`）。完整部署示例（Linux / Docker / K8s Secret）与 CLI / 沙箱脚本用法见下文「Basic 认证部署与使用」。

Python 请求示例：

```python
import requests

resp = requests.post(
    "http://127.0.0.1:8321/api/v1/proxies",
    json={
        "path_prefix": "/openai",
        "target_endpoint": "https://api.openai.com",
        "api_key": "sk-demo",
        "skip_cert_verify": False
    },
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "name": "openai",
  "state": "stopped",
  "created_at": "2026-04-25T11:40:00.000000"
}
```

创建 HTTP Basic 路由示例（Neo4j，密码从 Secret 文件读取）：

```python
resp = requests.post(
    "http://127.0.0.1:8321/api/v1/proxies",
    json={
        "path_prefix": "/neo4j",
        "target_endpoint": "http://neo4j.internal:7474",
        "basic_auth": {"username": "neo4j", "password_file": "/run/secrets/neo4j_password"}
    },
    timeout=30,
)
```

### 查询代理列表

接口：`GET /api/v1/proxies`

用途：列出全部代理路由。

Python 请求示例：

```python
import requests

resp = requests.get("http://127.0.0.1:8321/api/v1/proxies", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
[
  {
    "name": "openai",
    "state": "running",
    "listen_port": 18080,
    "route": {
      "path_prefix": "/openai",
      "target_endpoint": "https://api.openai.com",
      "api_key": "sk-demo...",
      "auth_type": "api_key",
      "basic_auth": null
    },
    "created_at": "2026-04-25T11:40:00.000000",
    "started_at": "2026-04-25T11:41:00.000000",
    "error_message": null
  },
  {
    "name": "neo4j",
    "state": "running",
    "listen_port": 18080,
    "route": {
      "path_prefix": "/neo4j",
      "target_endpoint": "http://neo4j.internal:7474",
      "api_key": "",
      "auth_type": "basic",
      "basic_auth": {
        "username": "neo4j",
        "password_configured": true,
        "password_file": "/run/secrets/neo4j_password"
      }
    },
    "created_at": "2026-04-25T11:40:00.000000",
    "started_at": "2026-04-25T11:41:00.000000",
    "error_message": null
  }
]
```

### 查询代理详情

接口：`GET /api/v1/proxies/{proxy_name}`

用途：查询单个代理路由详情。

Python 请求示例：

```python
import requests

proxy_name = "openai"
resp = requests.get(f"http://127.0.0.1:8321/api/v1/proxies/{proxy_name}", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "name": "openai",
  "state": "running",
  "listen_port": 18080,
  "route": {
    "path_prefix": "/openai",
    "target_endpoint": "https://api.openai.com",
    "api_key": "sk-demo",
    "skip_cert_verify": false,
    "target_host": "api.openai.com",
    "target_port": 443,
    "use_tls": true,
    "auth_type": "api_key",
    "basic_auth": null
  },
  "created_at": "2026-04-25T11:40:00.000000",
  "started_at": "2026-04-25T11:41:00.000000",
  "error_message": null
}
```

### 删除代理

接口：`DELETE /api/v1/proxies/{proxy_name}`

用途：删除代理路由。

Python 请求示例：

```python
import requests

proxy_name = "openai"
resp = requests.delete(f"http://127.0.0.1:8321/api/v1/proxies/{proxy_name}", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "name": "openai",
  "deleted": true
}
```

### 启动代理

接口：`POST /api/v1/proxies/{proxy_name}/start`

用途：启动代理路由。

Python 请求示例：

```python
import requests

proxy_name = "openai"
resp = requests.post(f"http://127.0.0.1:8321/api/v1/proxies/{proxy_name}/start", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "name": "openai",
  "state": "running",
  "started_at": "2026-04-25T11:41:00.000000",
  "error_message": null
}
```

### 停止代理

接口：`POST /api/v1/proxies/{proxy_name}/stop`

用途：停止代理路由。

Python 请求示例：

```python
import requests

proxy_name = "openai"
resp = requests.post(f"http://127.0.0.1:8321/api/v1/proxies/{proxy_name}/stop", timeout=30)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "name": "openai",
  "state": "stopped",
  "error_message": null
}
```

### 更新代理

接口：`PUT /api/v1/proxies/{proxy_name}`

用途：更新代理路由目标。

请求字段与创建代理相同，但 `path_prefix` 仅用于构造请求体，服务端会保留原有路由名对应的前缀。

Python 请求示例：

```python
import requests

proxy_name = "openai"
resp = requests.put(
    f"http://127.0.0.1:8321/api/v1/proxies/{proxy_name}",
    json={
        "path_prefix": "/openai",
        "target_endpoint": "https://api.openai.com/v1",
        "api_key": "sk-demo-new",
        "skip_cert_verify": False
    },
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

响应示例：

```json
{
  "name": "openai",
  "state": "running",
  "started_at": "2026-04-25T11:42:30.000000",
  "error_message": null
}
```

### 查看代理日志

接口：`GET /api/v1/proxies/{proxy_name}/logs`

用途：查看代理日志。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `lines` | integer/null | 否 | 只返回最后 N 行 |

Python 请求示例：

```python
import requests

proxy_name = "openai"
resp = requests.get(
    f"http://127.0.0.1:8321/api/v1/proxies/{proxy_name}/logs",
    params={"lines": 50},
    timeout=30,
)
print(resp.status_code)
print(resp.text)
```

响应示例：

```text
[2026-04-25T11:41:00.000000] Global proxy started on port 18080
[2026-04-25T11:41:00.100000] Route 'openai' enabled for routing
```

### Basic 认证部署与使用

> 本节补充 Basic 路由的端到端部署与用法。字段定义、REST 创建 / 更新示例见上文「创建代理」「更新代理」。

**安全模型要点：**

- 真实密码**只存在于服务端**：内联 `password`（明文，仅开发测试）或服务端可读的 `password_file`（生产推荐）。
- `password_file` 由 JiuwenBox 服务进程在**装配路由时**一次性读入内存；运行时转发不再触发文件 IO；CLI **不读取**该文件。
- 任何接口（列表 / 详情 / 日志 / 错误响应）都**不返回明文密码**，仅返回 `username` / `password_configured` / `password_file`。
- `api_key` 与 `basic_auth` 互斥；`password` 与 `password_file` 二选一。
- 客户端任何 `Authorization` 头（Bearer / Basic / 其他，大小写不敏感）都会被覆盖为唯一的 `Authorization: Basic ...`；`X-Api-Key` 不受影响。

**password_file 部署示例：**

Linux `0600` 文件：

```bash
install -m 0600 /dev/stdin /etc/jiuwenbox/secrets/neo4j_password <<'EOF'
replace-with-real-password
EOF
chown root:root /etc/jiuwenbox/secrets/neo4j_password
```

```yaml
inference_privacy_proxies:
  listen_port: 8322
  listen_host: "127.0.0.1"
  routes:
    - path_prefix: "neo4j"
      target_endpoint: "http://neo4j.internal:7474"
      basic_auth:
        username: "neo4j"
        password_file: "/etc/jiuwenbox/secrets/neo4j_password"
```

> 读取时仅去除结尾的一个换行；密码内部的空格 / 冒号等字符保留。CR / LF / NUL / 控制字符会被拒绝；空文件或仅换行的文件会被拒绝（`password_file is empty`）。

Docker Secret：

```bash
printf '%s' "replace-with-real-password" | docker secret create neo4j_password -
```

```yaml
# docker-compose.yml
services:
  jiuwenbox:
    image: jiuwenbox:latest
    secrets:
      - neo4j_password
    environment:
      JIUWENBOX_POLICY_PATH: /etc/jiuwenbox/policy.yaml
    volumes:
      - ./policy.yaml:/etc/jiuwenbox/policy.yaml:ro
secrets:
  neo4j_password:
    external: true
```

```yaml
# policy.yaml 引用 Secret 在容器内的挂载路径（默认 0444，服务端仍可读）
inference_privacy_proxies:
  listen_port: 8322
  listen_host: "0.0.0.0"
  routes:
    - path_prefix: "neo4j"
      target_endpoint: "http://neo4j.internal:7474"
      basic_auth:
        username: "neo4j"
        password_file: "/run/secrets/neo4j_password"
```

Kubernetes Secret：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: neo4j-basic-auth
type: Opaque
stringData:
  password: "replace-with-real-password"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: jiuwenbox
spec:
  template:
    spec:
      containers:
        - name: jiuwenbox
          image: jiuwenbox:latest
          env:
            - name: JIUWENBOX_POLICY_PATH
              value: /etc/jiuwenbox/policy.yaml
          volumeMounts:
            - name: policy
              mountPath: /etc/jiuwenbox/policy.yaml
              readOnly: true
            - name: neo4j-secret
              mountPath: /run/secrets
              readOnly: true
      volumes:
        - name: policy
          configMap: { name: jiuwenbox-policy }
        - name: neo4j-secret
          secret:
            secretName: neo4j-basic-auth
            defaultMode: 0400
```

```yaml
# policy.yaml 引用（K8s Secret 默认 0644，JiuwenBox 接受但记告警，建议 defaultMode: 0400）
inference_privacy_proxies:
  listen_port: 8322
  listen_host: "0.0.0.0"
  routes:
    - path_prefix: "neo4j"
      target_endpoint: "http://neo4j.internal:7474"
      basic_auth:
        username: "neo4j"
        password_file: "/run/secrets/password"
```

**CLI 创建 / 更新 Basic 路由：**

```bash
# --password-file（生产推荐；CLI 不读取文件，由服务端读取）
jiuwenbox proxy create \
  --prefix /neo4j --target http://neo4j.internal:7474 \
  --username neo4j --password-file /run/secrets/neo4j_password

# --password-stdin（密码走标准输入，不出现在进程参数中）
printf '%s' "$NEO4J_PASSWORD" | jiuwenbox proxy create \
  --prefix /neo4j --target http://neo4j.internal:7474 \
  --username neo4j --password-stdin

# --password（仅开发测试；可能暴露在 shell history / 进程参数中）
jiuwenbox proxy create \
  --prefix /neo4j --target http://neo4j.internal:7474 \
  --username neo4j --password dev-only-password

# 更新（同样支持三个密码来源，三选一；与 --api-key 互斥）
jiuwenbox proxy update neo4j \
  --prefix /neo4j --target http://neo4j.internal:7474 \
  --username neo4j --password-file /run/secrets/neo4j_password

# 查看脱敏详情
jiuwenbox proxy get neo4j
```

规则：`--password` / `--password-file` / `--password-stdin` 三选一；使用任一 Basic 选项时 `--username` 必填；`--api-key` 与 Basic 选项互斥。CLI 列表 / 详情 / 错误输出均不显示明文密码。

**沙箱内脚本调用上游 Neo4j：**

沙箱脚本**不携带任何凭据**，直接请求 Proxy；Proxy 注入 Basic 头后转发到上游。以 Neo4j 2026.06 HTTP 事务接口为例（`POST /db/{database}/tx/commit`，**不使用 Bolt 协议**）：

```python
# sandbox script: query_neo4j.py（无任何用户名/密码/Authorization）
import json, urllib.request

url = "http://127.0.0.1:8322/neo4j/db/neo4j/tx/commit"  # 经 JiuwenBox Proxy
payload = json.dumps({"statements": [{"statement": "RETURN 1 AS value"}]}).encode()
req = urllib.request.Request(
    url, data=payload, method="POST",
    headers={"Content-Type": "application/json", "Accept": "application/json"},
)
# 注意：不要设置 Authorization —— Proxy 会注入正确的 Basic 头并覆盖任意客户端 Authorization。
with urllib.request.urlopen(req, timeout=10) as resp:
    print(resp.status, resp.read().decode())
```

等价 `curl`（沙箱内）：

```bash
curl -s -X POST http://127.0.0.1:8322/neo4j/db/neo4j/tx/commit \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -d '{"statements":[{"statement":"RETURN 1 AS value"}]}'
```

## 说明

- `sandbox.exec` 和 `sandbox.exec_background` 中的 `workdir`、`env`、`timeout_seconds` 仍然有效。
- CLI 后台任务命令：`sandbox bg-exec`、`sandbox bg-get`、`sandbox bg-list`、`sandbox bg-kill`（除 `sandbox exec` 外，CLI 默认输出 JSON）
- 文档示例中的时间、PID、ID 仅为示意。
