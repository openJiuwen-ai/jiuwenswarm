# 面向 JetBrains 的 JiuwenSwarm —— 安装

## 前置条件

插件连接前，JiuwenSwarm 必须在本地运行：

```bash
jiuwenswarm-start
# WebSocket 服务器在 ws://127.0.0.1:19000/ws 打开
```

必须启用 JCEF（Chromium Embedded Framework）。如果首次打开时面板空白，请通过
**帮助 → 查找操作 → 注册表** → `ide.browser.jcef.enabled` 启用，然后重启 IDE。

## 安装

### 通过 ZIP（推荐）

1. 从[发布页](https://github.com/jiuwencortex/jiuwenswarm-ide/releases)下载
   `jiuwenswarm-plugin-0.1.0.zip`。
2. 进入 **设置 → 插件 → ⚙ → 从磁盘安装插件**，选择该 ZIP。
3. 重启 IDE。

### 通过应用商店

在 **设置 → 插件 → 应用商店** 中搜索 **JiuwenSwarm**，点击安装。
