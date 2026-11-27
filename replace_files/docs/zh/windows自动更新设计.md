# Windows 自动更新设计

本文档描述 JiuwenClaw 在 Windows 桌面版上的最小可用自动更新方案。目标是优先保证稳定性，而不是追求无感升级。

## 目标范围

- 仅支持 Windows 桌面版
- 启动时自动检查一次更新
- 用户可在左侧栏 `更新` 页面手动检查更新
- 更新源直接使用 GitHub Release
- 下载产物为 Inno Setup 安装包 `jiuwenclaw-setup-<version>.exe`
- 下载完成后由外部 helper 脚本静默安装并重启应用

## 不做的能力

- 不做增量更新
- 不做运行中自替换
- 不做 macOS 自动安装
- 不做版本忽略、灰度发布、多渠道
- 不做强制更新

## 核心流程

1. 应用启动后，前端异步调用 `updater.check`
2. 后端请求 GitHub Releases API 获取最新 release
3. 若发现新版本，则记录最新版本、发布时间、说明和安装包下载地址
4. 用户在 `更新` 页点击 `下载更新`
5. 后端后台下载安装包到 `%USERPROFILE%\\.jiuwenclaw\\.updates`
6. 下载完成后，前端调用 pywebview API 触发安装
7. 桌面进程写入临时 `cmd` helper，等待当前进程退出
8. helper 静默执行 Inno Setup 安装包，然后重启应用

## 更新源

默认使用 GitHub Releases API：

```text
https://api.github.com/repos/{owner}/{repo}/releases/latest
```

从 release 中读取：

- `tag_name` 作为最新版本号
- `body` 作为更新说明
- `published_at` 作为发布时间
- `assets[]` 中的安装包和可选 sha256 文件

## 配置

更新配置放在 `config.yaml` 的 `updater` 段：

```yaml
updater:
  enabled: true
  repo_owner: CharlieZhao95
  repo_name: jiuwenclaw
  release_api_url: ""
  asset_name_pattern: "jiuwenclaw-setup-{version}.exe"
  sha256_name_pattern: "jiuwenclaw-setup-{version}.exe.sha256"
  timeout_seconds: 20
```

## 后端接口

通过现有 WebSocket RPC 注册 3 个方法：

- `updater.get_status`
- `updater.check`
- `updater.download`

状态字段：

- `idle`
- `checking`
- `up_to_date`
- `update_available`
- `downloading`
- `downloaded`
- `installing`
- `error`
- `unsupported`

## 安装执行方式

为了避免主程序运行中替换自身文件，安装动作不在当前进程内完成。

桌面进程在接到前端安装请求后：

1. 生成临时 `cmd` helper 脚本
2. helper 等待桌面主进程退出
3. helper 运行安装包：

```text
/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /CLOSEAPPLICATIONS
```

4. 安装完成后重新启动 `jiuwenclaw.exe`
