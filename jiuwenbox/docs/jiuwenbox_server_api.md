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
  }
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
  }
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
    "env": {}
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
  "env": {}
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
  "env": {}
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
  "env": {}
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
  "env": {}
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
| `api_key` | string | 否 | 注入到上游的 API Key |
| `skip_cert_verify` | boolean | 否 | 是否跳过证书校验 |

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
      "api_key": "sk-demo..."
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
    "use_tls": true
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

## 说明

- `sandbox.exec` 和 `sandbox.exec_background` 中的 `workdir`、`env`、`timeout_seconds` 仍然有效。
- CLI 后台任务命令：`sandbox bg-exec`、`sandbox bg-get`、`sandbox bg-list`、`sandbox bg-kill`（除 `sandbox exec` 外，CLI 默认输出 JSON）
- 文档示例中的时间、PID、ID 仅为示意。
