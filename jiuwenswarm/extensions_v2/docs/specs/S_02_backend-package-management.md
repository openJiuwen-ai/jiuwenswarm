# 后端包管理协议

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 / 编号 | spec / S_02 |
| 最近修订 | 2026-11-04 |
| 所属模块 | `extensions_v2/management/adapter.py` |
| 特性依据 | [F_09](../features/F_09_backend-management-requests.md)、[F_10](../features/F_10_management-failure-recovery.md) |

门面契约沿用 [S_01](S_01_extension-management-facade.md)。

## 1. 请求

沿用 `AgentRequest` / `AgentResponse`，由已有认证、用户路由和 E2A 链路传递。

| 方法 | params | 行为 |
| --- | --- | --- |
| `extensions.v2.query` | 可选 `package_id: str` | 查询全部或一个包 |
| `extensions.v2.install` | `paths: list[str]`，非空，每项为现有 ZIP 文件的绝对路径 | 安装 ZIP 扩展包，拒绝目录 |
| `extensions.v2.uninstall` | `package_id: str` | 卸载包或未生效副本 |
| `extensions.v2.reload` | `package_id: str` | 从安装副本热重载，不先卸载 |

包标识沿用 Core 约束：1～128 个字符，首位为英文字母或数字，其余可包含英文字母、数字、点、
下划线和连字符，不允许空格或连续两个点。未知参数拒绝，不接受自建 Plan 或修改安装目录的参数。
外部安装请求先校验绝对路径、`.zip` 后缀和文件类型，再交给 Core 校验归档、manifest 与依赖。
此限制只在外部请求入口生效；启动通过 `load_installed()` 加载安装目录内的解压副本，不受影响。

## 2. 响应

保留原 `request_id`、`channel_id`、`metadata` 和 `agent_ref`；路径编码为字符串，元组编码为数组。

| 场景 | ok | payload |
| --- | --- | --- |
| 查询完成 | true | `state`：active、installed、installed_packages、inflight、published |
| 取得写操作结果 | true | `reports`：Core ActivationReport 数组 |
| 方法或参数错误 | false | `code=INVALID_PARAMS`、`error` |
| 门面尚不可用 | false | `code=EXTENSION_MANAGER_NOT_INITIALIZED`、`error` |
| 调用抛错 | false | `code`、`error`，不得伪造成功 Report |

Report 字段沿用 Core，`error` 编码为 `{code, message, params}`，不发送 StatusCode 枚举对象。
正常失败 Report 不补查、不重试。写操作抛错且未取得 Report 时返回 `outcome=unknown`；
核对成功附 `state`，核对失败附 `query_error`，原始 `code` 和 `error` 保留。
查询到当前状态不表示原操作成功，不补发 Report。查询请求失败不递归核对，取消继续传播。

安装响应另含 `installed_packages` 数组，各项为
`{package_id: str, version: str, install_path: str, generation: int}`。
只收录本次成功 Report 对应的生效记录，包 ID 和 generation 均须匹配。
`install_path` 是目标 AgentServer 上的实际安装绝对路径，来自记录的 `root`；不是最初的来源路径。
未取得匹配记录时不伪造目录，原始 Report 仍保留。失败 Report 不产生安装成功提示。

查询的 `state` 另含 `installed_packages`，各项为 `{package_id, version, install_path}`。
它从安装目录的固定布局取得 ID、版本和实际路径，不执行包代码；因此未生效的副本也可以展示 ID。
generation 保留为内部校验字段，不出现在命令结果中。

## 3. Web 命令

Web 在单 Agent 模式中解析 `/extension`，新建对话与已有会话均可使用，直接发起上述请求，不进入普通对话。
命令管理当前路由到的 AgentServer 进程内的扩展包，不只影响当前会话。

| 输入 | 请求与参数 |
| --- | --- |
| `/extension` | 显示帮助，不发请求 |
| `/extension install <ZIP 文件的绝对路径>` | `extensions.v2.install`，`paths` 为一个 ZIP 路径组成的数组 |
| `/extension query [package_id]` | `extensions.v2.query`，可选包 ID |
| `/extension uninstall <package_id>` | `extensions.v2.uninstall`，必填包 ID |
| `/extension reload <package_id>` | `extensions.v2.reload`，必填包 ID |

子命令不区分大小写，包 ID 和路径保留原文。单次命令安装一个 ZIP；install 后的完整内容作为一个路径，
允许直接粘贴含空格的路径，也兼容路径外的一对引号。反斜杠原样保留，不执行 shell 命令。
路径必须是运行服务可读取的绝对路径，最终校验由后端负责。

已有会话时 Web 携带 `session_id` 供 Gateway 路由；新建对话不携带前端占位标识 `new`，
不要求先创建聊天会话。两种情况都沿用连接认证和用户路由，不能因没有聊天会话绕过认证。
Gateway 将路由使用的会话标识移入 E2A 会话字段，不传入管理器业务参数。
Gateway 保留失败 payload。Web 按 Report.status 判断成功或失败；超时、断连或 `outcome=unknown`
显示结果未确认和查询用法，不自动重试。查询失败不展示为空列表。
安装、查询、卸载和重载结果明确标注 `package_id`。安装成功展示版本和安装目录，
提示修改安装副本后执行热重载；查询展示版本、状态和目录，不向用户显示进程名称或 generation。

## 4. 不变量与边界

1. 只使用已装配门面，不创建第二个 Bus 或 Registry。
2. 网络方法名与 Core 事件名分开，不接收候选对象或管理事件输入。
3. 查询不触发安装、回滚或重载；失败报告与没有报告的异常分开处理。
4. 不提供已发布版本回退或跨进程锁；Web 仅提供命令入口，不提供管理页面。

验证入口：`tests/unit_tests/extensions_v2/management/test_management_adapter.py`。

## 5. 本机 CLI

特性设计见 [F_31](../features/F_31_extension-cli.md)。源码、Wheel 使用 `jiuwenswarm extension`；
EXE、DMG 使用现有后端程序的 `extension` 分发，参数与结果使用同一实现。

| 命令参数 | 语义 |
| --- | --- |
| `install <ZIP>` | 读取最终 ZIP 的 ID、版本与 domain，再交给对应的运行宿主安装 |
| `query [package_id]` | 查询选定宿主；缺省 domain 为 agent |
| `reload <package_id>` | 从选定宿主的安装副本重载 |
| `uninstall <package_id>` | 卸载选定宿主中的包 |
| `--domain agent/gateway` | install 与 ZIP 声明必须一致；其它操作用于选择宿主 |
| `--json` | 结构化输出；当前默认也输出 JSON |
| `--timeout <seconds>` | 每次本机请求的超时，缺省 130，必须为正的有限数 |
| `--server-path <ZIP>` | 宿主已可读取的绝对路径；不执行上传 |

### 5.1 连接与授权

每个宿主在现有装配完成后创建回环监听，生成新的随机密钥和实例标识。
内部终端通过 `JIUWENSWARM_EXTENSION_CLI_AGENT` 或 `JIUWENSWARM_EXTENSION_CLI_GATEWAY` 继承连接记录；
Gateway 与 AgentServer 分别保存自己的记录，位置为数据目录下的
`run/extension-cli-<domain>-<实际服务端口>.json`；CLI 根据当前运行环境选择对应记录。
记录仅包含本机入口，CLI 不扫描端口，不读取浏览器登录信息。缺少当前实例的连接信息时返回 `HOST_UNAVAILABLE`。
记录在 POSIX 使用 0600 权限，Windows 沿用当前账号数据目录的访问权限。

本机入口只接受四种管理操作，校验随机密钥、实例标识与包的 domain 后，调用已装配的管理器。
该入口供当前操作系统账号使用，不提供远程用户授权；Web 请求继续使用原登录和路由规则。
关闭入口时撤销连接记录，旧连接信息不能用于重新启动的宿主。

### 5.2 结果与失败

CLI 安装前查询同 ID 状态；已有副本时不自动覆盖、卸载或重装。成功 Report 后再次核对版本、generation 与目录。
输出保留 `reports`、`before_state`、`state`，并分别记录 `operation_status`、`verification_status` 与总体 `status`。
`business_verification` 固定为 `not_run`；有页面通知时附 `ui_notified`，不将通知送达解释为页面已验收。

| 退出码 | 语义 |
| --- | --- |
| 0 | 查询成功，或写操作与后续核对均成功 |
| 1 | 明确失败报告，或成功报告与当前状态不符 |
| 2 | 输入、运行环境或写入前检查失败 |
| 3 | 写操作结果未知，或成功报告后的状态无法确认 |

超时、断线和无法识别的写操作结果仅查询一次当前状态，不重试写请求，不补造成功 Report。
安装成功后通知已有 WebUI 连接重新发送 ui.ready；未连接或通知失败不回滚安装。
未实际触发业务或检查浏览器时，CLI 结果不能单独证明用户效果已经完成。
