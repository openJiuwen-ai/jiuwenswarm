# 系统插件安装与状态核对

本 Skill 通过已有 `bash` 工具调用 Swarm 的 `extension` 命令。
运行中的 Swarm 自动提供当前实例的本机管理连接，用户无需在提问中提供端口、会话 ID 或认证信息。

本流程用于用户已明确要求安装，或已通过确认卡片同意安装的情况。
是否需要提问按 [主流程第 5.1 节](../SKILL.md#51-确认是否安装) 判断；用户不安装时，交付开发结果即可，不进入安装或界面效果确认。

## 1. 选择命令入口

| 当前运行方式 | 使用的入口 |
| --- | --- |
| 源码、Wheel | 虚拟环境已有的 `jiuwenswarm extension` |
| EXE、DMG | `JIUWENSWARM_EXECUTABLE` 指向的现有后端程序，后接 `extension` |

工具名 `bash` 表示终端执行能力，不要求每台机器都使用 POSIX Bash。
Windows 可选择 PowerShell；按当前终端语法正确引用程序路径，保留路径中的空格。

源码、Wheel 示例：

```text
jiuwenswarm extension install "<最终 ZIP 路径>" --json
jiuwenswarm extension query "<package_id>" --json
```

桌面包使用 PowerShell 时：

```powershell
& $env:JIUWENSWARM_EXECUTABLE extension install "<最终 ZIP 路径>" --json
```

桌面包使用 Bash 时：

```bash
"$JIUWENSWARM_EXECUTABLE" extension install "<最终 ZIP 路径>" --json
```

选择入口时注意：

- **桌面程序：** 使用运行环境给出的后端路径，不猜 APP 名称，不另找系统 Python，也不新增通用 CLI 启动器。
- **可用性：** 可先执行对应入口的 `extension --help`，再执行 query。旧版本不识别命令时说明版本缺口。
- **环境边界：** 内部 agent 的终端继承当前宿主环境；外部独立终端和远程沙箱不一定有相同的运行上下文。

## 2. 确认当前实例和文件

| 检查项 | 要求 |
| --- | --- |
| 安装目标 | 使用当前任务所在的 Swarm 实例，不扫描端口或改连其它实例 |
| 管理授权 | 由宿主生成并校验本机管理访问密钥；不读取或展示内部连接变量与密钥 |
| 系统插件的归属 | install 从 `manifest.json` 的 `package.domain` 确定；只能为 `agentserver` 或 `gateway` |
| ZIP 文件 | 运行服务可以读取同一份最终 ZIP；本机路径不能直接当作远程沙箱路径 |

- **影响范围：** 安装影响目标宿主进程的扩展能力，不只影响当前对话。
- **自动连接：** CLI 读取宿主提供的连接上下文或当前实例确定的运行记录，不要求用户填写连接参数。
- **连接缺失：** 返回 `HOST_UNAVAILABLE` 时说明宿主未启动、版本不支持或终端未获得运行上下文；保留已完成的源码和 ZIP，不另写客户端或搜索凭据。
- **沙箱场景：** 本机通道不负责沙箱网络和文件映射。需要已有、已授权的传输能力；缺少时如实记录，不能把打包完成写成安装完成。

## 3. 安装最终 ZIP

先完成最终 ZIP 的隔离加载与效果测试，再安装同一份文件。
CLI 按以下步骤执行：

1. 读取根目录 manifest，取得 ID、版本和 domain，计算本地 SHA-256，不导入系统插件代码。
2. 连接对应宿主，先查询该系统插件。已有同 ID 的系统插件或安装副本时返回冲突，不自动卸载或覆盖。
3. 发送一次安装请求，保留宿主原始 Report。
4. 取得成功报告后再次查询，核对 ID、版本、generation 和安装目录。
5. 输出 JSON 和退出码。安装成功后由宿主通知现有页面，实际业务效果另行验证。

若最终 ZIP 已通过已有方式放到宿主另一位置，可使用 `--server-path "<宿主 ZIP 绝对路径>"`。
命令不上传文件；输出的 `zip_sha256` 只代表本地最终 ZIP，不能据此声称服务端核对了同一摘要。

## 4. 查询、重载和卸载

以下示例使用源码、Wheel 入口；桌面包替换命令前缀，参数不变：

```text
jiuwenswarm extension query "<package_id>" --json
jiuwenswarm extension reload "<package_id>" --json
jiuwenswarm extension uninstall "<package_id>" --json
```

Gateway 系统插件的后续操作增加 `--domain gateway`，例如：

```text
jiuwenswarm extension query "<package_id>" --domain gateway --json
```

- **查询：** 未指定系统插件 ID 时查询选定宿主；默认 `agentserver`，不把查询失败展示成空列表。
- **重载：** 读取已安装副本。修改源目录或 ZIP 后，不会自动更新安装副本。
- **更新：** 当前 CLI 遇到已安装的同 ID 系统插件时会拒绝 install，不支持用新 ZIP 直接覆盖。本轮首次安装成功后会返回安装目录；后续修改先更新源码、重新打包并验证最终 ZIP，再将变动同步到这个安装目录中的同一系统插件，最后执行 reload。不要卸载重装，也不要把来源目录或 ZIP 的改动误当作已经生效。
- **未知结果：** 超时、断连或报告不明确时只核对状态，不重复发送写请求。

## 5. 读取结果并验证效果

| 字段 | 含义 |
| --- | --- |
| `status` / `ok` | 本次命令的总体结果 |
| `domain` | 实际选择的宿主 |
| `operation_status` | 原始管理操作的结果 |
| `verification_status` | 操作后的状态核对结果 |
| `reports` | 宿主原始报告 |
| `state` / `before_state` | 操作后的查询状态与操作前状态，不能混用 |
| `install_path` | 当前生效副本的实际安装目录 |
| `ui_notified` | 首次安装后的页面通知是否送达。`true` 会让已就绪页面重新发送 `ui.ready`，不等于真实页面已验收；它只用于技术核对，不要求智能体启动或查找浏览器 |
| `business_verification` | CLI 固定为 `not_run`，业务效果由后续验证确认 |

| 退出码 | 含义 | 后续处理 |
| --- | --- | --- |
| 0 | 查询成功，或写操作和状态核对均成功 | 按系统插件的要求验证业务效果 |
| 1 | 已确认失败，或成功报告与当前状态不符 | 阅读报告，处理具体原因 |
| 2 | 输入、连接上下文或写入前检查失败 | 处理缺口，不宣称已安装 |
| 3 | 写操作结果或后续状态尚未确认 | 保留原结果，仅查询，不重复写入 |

安装后使用原有业务入口验证需求。带界面的系统插件检查现有槽位与页面，依赖业务事件的 Handler 等下一次正常操作触发。
安装或必要业务操作成功后，直接通过当前环境提供的提问能力向用户确认实际效果；不启动、查找、刷新或等待浏览器来替代该确认。用户提出修改意见后，更新安装目录内的当前系统插件并执行 reload，再次提问，直到用户确认；不要卸载后重新安装。
