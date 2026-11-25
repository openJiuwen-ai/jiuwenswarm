# ACP 快速启动

本文将介绍如何在本机启动 `jiuwenswarm` 主进程，并通过 VS Code ACP Client 连接使用。

## 前置要求

* Python `>=3.11`
* 已安装 VS Code 扩展 `formulahendry.acp-client`



---

## 启动顺序

ACP 依赖本地 Gateway，**必须先启动主进程，再在 VS Code 中连接 Agent**。

顺序如下：

1. 安装 `workswarm`
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

pip install workswarm
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

> 说明：`jiuwenswarm-acp` 是 pip install 后自动生成的命令，与 `jiuwenswarm-init`、`jiuwenswarm-start` 同级。需确保 VS Code 在已安装 workswarm 的虚拟环境中运行，否则需填写完整路径，例如 Windows：`C:\path\to\venv\Scripts\jiuwenswarm-acp.exe`，Linux / macOS：`/path/to/venv/bin/jiuwenswarm-acp`。

![ACP配置](../assets/images/current-ui/09-Harness页面.png)

### 6. 建立连接

完成上述配置后，在 ACP Client 中连接 jiuwenswarm Agent 即可开始使用。

![ACP配置完成](../assets/images/ACP配置完成.png)
---

## 安装 Multica 原生 JiuwenSwarm Provider

1. 在 Multica daemon 使用的环境中安装带原生 ACP 入口的 `workswarm`，运行 `jiuwenswarm-acp --version` 检查安装结果。
2. 启动 JiuwenSwarm Gateway 并配置可用模型。如果 Gateway 使用自定义地址，在 daemon 的环境中设置 `GATEWAY_URL`。默认地址为 `ws://127.0.0.1:19001/acp`。
3. 使用支持 `jiuwenswarm` Provider 的 Multica 版本。将 `jiuwenswarm-acp` 加入 daemon 的 PATH，或将 `MULTICA_JIUWENSWARM_PATH` 设为该命令的绝对路径，然后重启 daemon。
4. 为 Multica Agent 绑定自动发现的 JiuwenSwarm 运行时，分配一个带仓库检出的测试事项，确认 Agent 能修改该仓库中的文件。

## 返回导航

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)

原生 Provider 通过 `configOptions` 公布当前模型的思考档位。所选档位应用于当前 ACP 连接的后续提示，选择「默认」可恢复模型原有配置。AgentServer 需要运行包含此功能的 JiuwenSwarm 版本。

Multica MCP 配置中，`type: "http"` 使用 Streamable HTTP，`type: "sse"` 使用 SSE。请填写服务地址，按需添加请求头。本地 ACP 进程为会话建立这些连接，并在加载会话时根据本次下发的 MCP 配置重新连接。
