# ACP 快速启动

`jiuwenswarm` 实现了 [Agent Client Protocol (ACP)](https://agentclientprotocol.com/)，可从任意支持 ACP 的编辑器接入（VS Code、Zed、Neovim 等）。本文将介绍如何在本机启动 `jiuwenswarm` 主进程，并通过 ACP 客户端连接使用。

## 前置要求

* Python `>=3.11, <3.14`
* 已安装 VS Code 扩展 `formulahendry.acp-client`



---

## 启动顺序

ACP 依赖本地 Gateway，**必须先启动主进程，再在 VS Code 中连接 Agent**。

顺序如下：

1. 安装 `jiuwenswarm`
2. 执行 `jiuwenswarm-init`
3. 配置大模型相关信息
4. 启动主进程
5. 在 VS Code 中配置 ACP Agent
6. 连接 Agent 开始使用

---

## 方式一：源码启动

适用于已 clone 仓库的场景。

### 1. 安装依赖

在仓库根目录执行：

```bash
uv venv --python=3.11

#激活虚拟环境
# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

uv sync
```

### 2. 初始化

```bash
jiuwenswarm-init
```

### 3. 配置大模型信息

执行 `jiuwenswarm-init` 后，需要按项目要求**配置大模型相关信息**，否则 Agent 无法正常推理。配置方法参考: [配置方法](配置信息.md)

### 4. 启动主进程

```bash
python -m jiuwenswarm.app
```

### 5. 在 VS Code 中配置 ACP

在 ACP Client 插件中执行 **ACP: Add Agent Configuration**，然后填写：

* **Name**：`jiuwenswarm`
* **Command**：

  * Windows：`<repo>/scripts/run_gateway_acp.cmd`
  * Linux / macOS：`<repo>/scripts/run_gateway_acp.sh`
* **Config / Arguments**：留空

> 说明：仓库脚本默认使用仓库根目录下的 `.venv`。

![ACP配置](../assets/images/current-ui/09-Harness页面.png)


### 6. 建立连接

完成上述配置后，在 ACP Client 中连接 jiuwenswarm Agent 即可开始使用。

![ACP配置完成](../assets/images/ACP配置完成.png)

---

## 方式二：Wheel 启动

适用于直接通过 Wheel 包安装启动的场景。

### 1. 安装

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

pip install jiuwenswarm
```

### 2. 初始化

```bash
jiuwenswarm-init
```

### 3. 配置大模型信息

执行 `jiuwenswarm-init` 后，需要按项目要求**配置大模型相关信息**，否则 Agent 无法正常推理。配置方法参考: [配置方法](配置信息.md)

### 4. 启动主进程

```bash
python -m jiuwenswarm.app
```

### 5. 在 VS Code 中配置 ACP

在 ACP Client 插件中执行 **ACP: Add Agent Configuration**，然后填写：

* **Name**：`jiuwenswarm`
* **Command**：`jiuwenswarm-acp`
* **Config / Arguments**：留空

> 说明：`jiuwenswarm-acp` 是 pip install 后自动生成的命令，与 `jiuwenswarm-init`、`jiuwenswarm-start` 同级。需确保 VS Code 在已安装 jiuwenswarm 的虚拟环境中运行，否则需填写完整路径，例如 Windows：`C:\path\to\venv\Scripts\jiuwenswarm-acp.exe`，Linux / macOS：`/path/to/venv/bin/jiuwenswarm-acp`。

![ACP配置](../assets/images/current-ui/09-Harness页面.png)

### 6. 建立连接

完成上述配置后，在 ACP Client 中连接 jiuwenswarm Agent 即可开始使用。

![ACP配置完成](../assets/images/ACP配置完成.png)
---

## 返回导航

---

## Zed 配置

Zed 内置 ACP 支持（External Agents）。

1. 先启动主进程：`python -m jiuwenswarm.app`
2. 在 Zed 的 `settings.json` 中添加（或通过 Agent Settings → External Agents → Add Custom Agent）：

```json
{
  "agent_servers": {
    "JiuwenSwarm": {
      "type": "custom",
      "command": "jiuwenswarm-acp",
      "args": []
    }
  }
}
```

3. 打开 Agent Panel，选择 `JiuwenSwarm` 新建会话。

> 若 Zed 的 PATH 中找不到 `jiuwenswarm-acp`，请填写完整路径（参考上文说明）。调试连接可在 Command Palette 中执行 `dev: open acp logs`。

---

## Neovim 配置（CodeCompanion.nvim）

[CodeCompanion.nvim](https://codecompanion.olimorris.dev/) 支持 ACP Agent，将 `jiuwenswarm-acp` 注册为自定义 ACP adapter：

```lua
require("codecompanion").setup({
  adapters = {
    acp = {
      jiuwenswarm = function()
        return require("codecompanion.adapters").extend("claude_code", {
          name = "jiuwenswarm",
          formatted_name = "JiuwenSwarm",
          commands = {
            default = { "jiuwenswarm-acp" },
          },
          defaults = {},
          env = {},
        })
      end,
    },
  },
  interactions = {
    chat = { adapter = "jiuwenswarm" },
  },
})
```

先启动主进程（`python -m jiuwenswarm.app`），再打开 CodeCompanion 对话。不同版本插件的配置键可能不同（旧版本使用 `strategies` 而非 `interactions`），请以插件文档为准。

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)
