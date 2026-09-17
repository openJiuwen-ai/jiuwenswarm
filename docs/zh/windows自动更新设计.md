# 桌面端自动更新设计

本文档描述 JiuwenSwarm 桌面版（Windows 与 macOS）的自动更新方案。目标是优先保证稳定性，同时覆盖正式版与预发布版（beta）的升级流程。

## 目标范围

- 支持 Windows 与 macOS 桌面版（Linux 桌面版同样适用）
- 启动时自动检查一次更新
- 用户可在左侧栏 `更新` 页面手动检查更新
- 桌面版更新源默认使用 GitCode Releases，可切换为 GitHub Releases；pip 安装模式使用 PyPI
- 下载产物按平台区分：
  - Windows：Release 中的 `.exe` 安装包；存在多个候选时，优先选择文件名中唯一包含 `workswarm` 的包
  - macOS：Release 中的 `.dmg` 镜像；存在多个候选时，优先选择文件名中唯一包含 `workswarm` 的包
  - Linux：仍按 `JiuwenSwarm-<version>.tar.gz` 精确匹配
- 下载完成后由外部 helper 完成安装与重启：Windows 以交互式安装向导完成，macOS / Linux 由 helper 脚本静默安装并重启
- 支持预发布版本：稳定版与预发布版共用同一更新通道，稳定版用户也会收到 beta 推送

## 不做的能力

- 不做增量更新
- 不做运行中自替换
- 不做版本忽略、灰度发布、多渠道分流
- 不做强制更新

## 桌面版时间戳规则

同一个版本的 Windows 与 macOS 安装包一起发布。

Windows 与 macOS 桌面端从分页的 Releases 列表中找到当前安装版本对应的发布记录，将其发布时间与列表中最新的发布时间比较。若列表未包含当前版本，则按 tag 补查当前 Release；时间戳统一转换为 UTC，只有远端时间更新时才提示更新。Linux 保持原有版本比较逻辑。

版本号仅用于定位当前 Release 和界面展示，不参与桌面版本的新旧排序，也不参与 Windows / macOS 安装包匹配。每个 Release 应分别只包含一个 `.exe` 和一个 `.dmg`；作为临时过渡兼容规则，同平台存在多个安装包时，自动更新会选择文件名中唯一包含 `workswarm`（不区分大小写）的包。其他附件可使用任意名称。`pip` 安装模式仍保持原有版本比较逻辑。

## 核心流程

1. 应用启动后，前端异步调用 `updater.check`
2. 后端请求 Releases 列表接口，获取全部已发布版本（含预发布，跳过 draft）
3. 按发布时间选出最新 Release，与当前安装版本对应的 Release 时间比较
4. 若发现新版本，记录最新版本、发布时间、更新说明和对应平台的安装包下载地址
5. 用户在 `更新` 页点击 `下载更新`
6. 后端后台下载安装包到用户工作区下的 `.updates` 目录
7. 下载完成后，前端调用 pywebview API `install_update` 触发安装
8. 桌面进程拉起平台对应的 helper，等待当前进程与端口释放后执行安装（Windows 交互式、macOS / Linux 静默）并重启应用

## 更新源

桌面版默认使用 GitCode Releases 列表接口：

```text
https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases
```

也可切换为 GitHub Releases。为了发现预发布版本，后端拉取完整 releases 列表（而非 `/latest` 端点，因为 `/latest` 会排除预发布），跳过 draft、保留 prerelease，再按发布时间取最新。当列表接口不可用时回退到 `/latest`。

从 release 中读取：

- `tag_name` 作为版本号（保留预发布后缀，如 `0.2.3.beta1`）
- `body` 作为更新说明
- `published_at` 作为发布时间
- `assets[]` 中按平台扩展名匹配的安装包（Windows `.exe`、macOS `.dmg`）；存在多个候选时优先选择文件名中唯一包含 `workswarm` 的包

## 配置

更新配置放在 `config.yaml` 的 `updater` 段：

```yaml
updater:
  enabled: true
  desktop_release_api_type: gitcode   # gitcode | github
  repo_owner: openJiuwen
  repo_name: jiuwenswarm
  release_api_url: ""
  asset_name_pattern_linux: "JiuwenSwarm-{version}.tar.gz"
  timeout_seconds: 20
```

Windows / macOS 不再读取安装包命名模板；旧配置中的相关字段会保留兼容读取，但不影响选包。pip 安装模式额外支持 `pypi_mirror` 字段。

## 后端接口

通过 WebSocket RPC 注册以下方法：

- `updater.get_status` — 查询当前更新状态
- `updater.check` — 检查更新
- `updater.download` — 下载安装包（desktop 模式）/ 执行 pip 升级（pip 模式）
- `updater.upgrade` — 仅 pip 模式使用，执行升级并重启
- `updater.set_conf` — 保存更新配置

桌面模式下的安装由前端通过 pywebview API `install_update(installer_path)` 触发，由桌面进程直接执行（它持有窗口并能在安装前关闭窗口）。

状态字段：

- `idle`
- `checking`
- `up_to_date`
- `update_available`
- `downloading`
- `downloaded`
- `installing`
- `upgrading`（pip 模式）
- `restart_pending` / `restarting`（pip 模式）
- `error`
- `unsupported`
- `disabled`

## 安装执行方式

为了避免主程序运行中替换自身文件，安装动作不在当前进程内完成。桌面进程在接到前端安装请求后，按平台拉起独立的 helper 进程/脚本，helper 在主进程退出后完成安装与重启。

### Windows

桌面进程通过子命令 `update-helper` 拉起一个独立的更新助手进程，传入安装包路径、应用可执行路径与父进程 PID。助手流程：

1. 等待父进程退出
2. 等待后端 / 前端端口释放（最多 15 秒）
3. 以交互式方式启动安装包（不带静默参数），弹出 Inno Setup 安装向导

安装包由 Inno Setup 自身处理提权（UAC 弹窗）与文件替换，用户在向导中完成安装后由安装包负责重启应用（Inno Setup 的 `[Run]` 段可配置安装完成后启动应用）。助手在启动安装包后即退出，安装过程交由用户与安装包完成。

### macOS

桌面进程生成一个 bash helper 脚本并独立启动。脚本流程：

1. 等待父进程退出
2. 等待后端 / 前端端口释放（最多 15 秒）
3. `hdiutil attach` 将 DMG 挂载到受控挂载点
4. 在挂载点内查找 `.app` 包
5. `ditto` 将 `.app` 拷贝到临时目标 `<install_target>.new`
6. 原子交换：将旧包移为 `<install_target>.old`、新包就位、删除旧包
7. `hdiutil detach` 卸载 DMG 并清理挂载点
8. `xattr -dr com.apple.quarantine` 去除隔离属性
9. `open` 启动新应用

安装目标固定为 `/Applications/JiuwenSwarm.app`（从可执行路径上溯到 `.app` 包名推导）。

### Linux

桌面进程生成 bash helper 脚本，等待父进程退出后：备份当前安装目录、解压 tar.gz 到安装目录、删除备份、重启 `jiuwenswarm`。

## 安全说明

- 所有外部路径在 helper 脚本中均使用 `shlex.quote` 转义，防止 release 接口返回恶意资源名时发生 shell 注入
- helper 脚本写入用户工作区下的 `.updates` 目录，写入前检查写权限
- macOS helper 将完整执行日志写入日志目录的 `update_helper.log`
---

## 返回导航

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)