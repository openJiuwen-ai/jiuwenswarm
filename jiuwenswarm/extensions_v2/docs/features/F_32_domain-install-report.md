# AgentServer 按域加载与安装报告

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 / 编号 | feature / F_32 |
| 最近修订 | 2026-11-06 |
| 负责人 | 李志远 |
| 关联计划 | [EXTV2-32](../designs/extensions-v2-development-plan.md#extv2-32-agentserver-按域启动与共享安装报告) |
| 状态 | 已实现 |

## 1. 概述

扩展包在 `manifest.json` 的 `package.domain` 中声明运行域。当前支持 `agentserver` 和 `gateway`；
缺失或使用其它值的声明会在安装时被拒绝。

AgentServer 的实现仍位于 `jiuwenswarm/extensions_v2`，启动入口仍为 `assembly.install_extensions`。
初始化时，门面向 Core 传入 `domain="agentserver"` 和可识别的 domain 集合。Core 只处理通用的按域加载能力。

## 2. 配置和初始化

```yaml
extensions:
  install_dir: extension_pkgs
  data_dir: extension
  install_report:
    poll_interval_ms: 500
    dependency_timeout_seconds: 300
    lock_timeout_seconds: 5
```

`data_dir` 的相对路径以用户工作目录为基准，也支持绝对路径和用户目录展开。报告文件名固定为
`extension_install_report.yaml`，默认位置为 `~/.jiuwenswarm/extension/extension_install_report.yaml`。

```python
ExtensionManager(
    install_dir: Path, *, report_path: Path,
    poll_interval: float = 0.5, dependency_timeout: float = 300.0, lock_timeout: float = 5.0,
    store_dir: Path | None = None, state_dir: Path | None = None,
)
```

报告文件由 Core 的 `PackageInstallReport` 维护。它记录包的安装结果和当前版本，并通过进程锁协调
同一安装根目录中的包操作。Swarm 只负责解析配置并传入参数。

## 3. 启动加载

启动时，Swarm 只向 Core 传入安装根目录，不再预先选择包路径。加载顺序如下：

1. Core 在根目录发现全部已安装包，校验 manifest，选择可用版本，并按依赖关系生成稳定顺序。
2. Core 按 `package.domain` 从该顺序中选择 `agentserver` 包。
3. 每个包安装前，Core 根据安装报告确认其依赖的结果；跨域依赖尚未完成时，按配置轮询等待。
4. 安装结果写入报告，并返回对应的 `ActivationReport`。

非法声明或依赖失败会返回失败报告。与其无依赖关系的包继续处理。

## 4. 管理、查询和页面恢复

安装、重载和卸载都经过 Core 的报告保护。等待依赖时不占用 Swarm 门面锁；初始化、卸载和停机仍由
本地锁保护。

无参数的 `/extension query` 按包 ID 显示共享报告中的包。每个包显示 ID、domain 和当前生效版本；
属于当前进程的包还在同一块中显示运行状态和安装目录。指定包 ID 的查询使用相同的展示规则。

包安装完成后，管理适配器通知已挂载的页面重新发送 `ui.ready`。热重载时，旧代际的视图先发送
`webui.render.clear`，清理完成后再发送同一通知，因此页面会为新代际重新发送 `ui.ready`，无需刷新浏览器。
通知事件名为 `extensions.v2.installed`，通知中携带需要重新就绪的 `package_ids` 和 `session_id`。

示例包和测试夹具都在 `package` 下填写 `domain`。`auth_login_demo` 使用 `gateway`，其余现有示例使用
`agentserver`。本机 CLI 从 `package.domain` 选择目标宿主；`--domain` 仅接受 `agentserver` 或 `gateway`。

## 5. 验证

2026-11-06 在本地 Core 可编辑安装的环境中执行：

```text
uv run pytest tests/unit_tests/extensions_v2 --asyncio-mode=auto -q
npm run test:webui-runtime
npm run test:webui-transport
npm run test:extension-command
```

Extensions V2 回归结果为 1133 passed、2 skipped；WebUI 运行时 19 项、传输 9 项、Web 命令 69 项通过。
回归覆盖按域加载、报告参数、示例声明、CLI 查询、安装、卸载、重载和浏览器重新就绪。
