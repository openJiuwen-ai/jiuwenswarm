# SandboxClient 接口总结

`SandboxClient` 提供了与沙箱服务交互的多种方法，包括沙箱的生命周期管理、命令和代码执行、以及文件传输功能。

## 1. 沙箱生命周期管理

### `create_sandbox()`

- **功能**: 创建一个新的沙箱实例。
- **返回**: `ExecutionResult`，包含沙箱ID。

### `delete_sandbox(sandbox_id: str)`

- **功能**: 删除指定的沙箱。
- **参数**: 
  - `sandbox_id`: 沙箱的唯一标识符。
- **返回**: `ExecutionResult`，表示操作是否成功。

### `refresh_duration(sandbox_id: str, duration_seconds: int | None = None)`

- **功能**: 续期指定的沙箱。
- **参数**: 
  - `sandbox_id`: 沙箱的唯一标识符。
  - `duration_seconds`: 续期的秒数，如果为 `None`，则使用配置中的默认值。
- **返回**: `ExecutionResult`，表示操作是否成功。

## 2. 执行命令和代码

### `exec_command(sandbox_id: str, command: str, timeout_seconds: int | None = None)`

- **功能**: 在沙箱中执行Shell命令。
- **参数**: 
  - `sandbox_id`: 沙箱的唯一标识符。
  - `command`: 要执行的命令。
  - `timeout_seconds`: 命令执行的超时时间，如果为 `None`，则使用配置中的默认值。
- **返回**: `ExecutionResult`，包含命令执行结果。

### `exec_code(sandbox_id: str, code: str, language: str = "python", timeout_seconds: int | None = None)`

- **功能**: 在沙箱中执行代码。
- **参数**: 
  - `sandbox_id`: 沙箱的唯一标识符。
  - `code`: 要执行的代码。
  - `language`: 代码语言，默认为Python。
  - `timeout_seconds`: 代码执行的超时时间，如果为 `None`，则使用配置中的默认值。
- **返回**: `ExecutionResult`，包含代码执行结果。

## 3. 文件传输

### `upload_file(local_path: str, remote_path: str, sandbox_id: str, **kwargs)`

- **功能**: 上传文件到沙箱。
- **参数**: 
  - `local_path`: 本地文件路径。
  - `remote_path`: 沙箱中的目标路径。
  - `sandbox_id`: 沙箱的唯一标识符。
- **返回**: `ExecutionResult`，表示操作是否成功。

### `download_file(remote_path: str, sandbox_id: str)`

- **功能**: 从沙箱下载文件。
- **参数**: 
  - `remote_path`: 沙箱中的源文件路径。
  - `sandbox_id`: 沙箱的唯一标识符。
- **返回**: `ExecutionResult`，包含文件内容（Base64编码）。

## 4. 辅助功能

### `list_sandbox_files(sandbox_id: str, root: str = ".")`

- **功能**: 列出沙箱内的文件。
- **参数**: 
  - `sandbox_id`: 沙箱的唯一标识符。
  - `root`: 搜索的根目录，默认为当前目录。
- **返回**: 文件路径列表。

### `close()`

- **功能**: 关闭HTTP客户端，释放资源。

---

# SandboxConfig 字段总结

`SandboxConfig` 是一个数据类，用于配置沙箱服务的连接和行为。

## 字段

- `api_base`: 沙箱服务地址（协议+域名，不含尾斜杠），如 `https://sandbox.example.com`。
- `template_id`: 沙箱模板ID，用于 `x-sandbox-template-id` 请求头。
- `duration_seconds`: 沙箱存活时长（秒），创建/续期时使用，默认为900秒。
- `timeout_seconds`: HTTP请求超时（秒），默认为120秒。
- `metadata`: 创建沙箱时传入的metadata（如teamid/userid），默认为空字典。
- `command_timeout_seconds`: 执行命令的默认超时（秒），默认为60秒。
- `code_timeout_seconds`: 执行代码的默认超时（秒），默认为60秒。