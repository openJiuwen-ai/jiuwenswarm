# 集成 JiuwenSwarm

> 本篇讲解如何将 AgentSSAS 接入 JiuwenSwarm：通过应用 patch 注册 AgentSSASSecurityRail，让 Agent 行为事件被自动采集并执行威胁检测。适合需要在 JiuwenSwarm 中启用安全态势感知的用户。

## 集成原理

AgentSSAS 对 JiuwenSwarm 的接入通过一个 patch 完成。应用 patch 后，`AgentSSASSecurityRail` 会被注册到 JiuwenSwarm 的 DeepAgent 执行链路，自动采集 Agent 行为事件并上报给 AgentSSASCore 分析引擎。

patch 修改 JiuwenSwarm 仓库的 3 个文件：

| 文件 | 修改内容 |
| ---- | -------- |
| `jiuwenswarm/resources/config.yaml` | 新增 `ssas` 配置段（配置模板） |
| `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py` | 可选导入 AgentSSASSecurityRail，新增 `_build_ssas_rail` 构建方法并注册 Rail |
| `pyproject.toml` | 声明 `agent-ssas` 依赖 |

## 前提

- Python >= 3.11，已安装 [uv](https://docs.astral.sh/uv/)
- 已有 JiuwenSwarm 源码仓（develop 分支）与 AgentSecurity 仓

**本地依赖版**（`jiuwenswarm_ssas_local.patch`）要求两个仓库同级存放：

```text
<workspace>/
├── jiuwenswarm/          # JiuwenSwarm 仓库(develop 分支)
└── AgentSecurity/
    └── AgentSSAS/        # 本仓库
```

**在线依赖版**（`jiuwenswarm_ssas_online.patch`）从 atomgit 拉取 `agent-ssas`，不要求两仓同级存放，任意路径的 jiuwenswarm 仓库都可应用。

## 第一步：应用 patch（二选一）

在 jiuwenswarm 仓库根目录执行：

```bash
# 方式 A: 本地依赖版 —— editable 引用本地 AgentSSAS 源码,改源码立即生效,适合开发调试
git apply ../AgentSecurity/AgentSSAS/patches/jiuwenswarm_ssas_local.patch

# 方式 B: 在线依赖版 —— 从 atomgit 拉取 agent-ssas,适合正式使用
git apply ../AgentSecurity/AgentSSAS/patches/jiuwenswarm_ssas_online.patch
```

两个 patch 对 3 个文件的修改内容完全一致，区别只在 `pyproject.toml` 中 `agent-ssas` 依赖的来源：

- 本地依赖版声明为 `agent-ssas = { path = "../AgentSecurity/AgentSSAS", editable = true }`
- 在线依赖版声明为 `agent-ssas @ git+https://atomgit.com/yieux1/AgentSecurity.git@develop#subdirectory=AgentSSAS`

## 第二步：安装依赖

在 jiuwenswarm 仓库根目录执行：

```bash
uv sync
```

## 第三步：确认配置

patch 在 jiuwenswarm 的 `resources/config.yaml`（配置模板）中加入了 `ssas` 段：

```yaml
ssas:
  enabled: true            # 总开关,关闭后不注册 AgentSSASSecurityRail
  mode: inprocess          # inprocess(进程内) / http(独立 HTTP 服务)
  decision_policy: observe_only   # observe_only(仅感知) / active_protection(按风险处置)
  # ...完整字段见 docs/zh/reference/configuration.md
```

- **新用户**：执行 `jiuwenswarm-init` 时模板会复制到 `~/.jiuwenswarm/config/config.yaml`，无需额外操作
- **已有用户**：`~/.jiuwenswarm/config/config.yaml` 已存在（不会被模板覆盖），请手动把 `ssas` 段加入该文件，并将 `enabled` 设为 `true`

> 注意：用户 config.yaml 中默认不包含 `ssas` 段。必须显式添加并设置 `enabled: true` 才能启用 SSAS。

## 第四步：启动验证

```bash
jiuwenswarm-init      # 首次使用时初始化
jiuwenswarm-start     # 启动,浏览器访问 http://localhost:5173
```

启动日志中出现以下行即说明集成成功：

```text
[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail create success, mode=inprocess, policy=observe_only
```

与 Agent 对话（任意触发工具调用的任务），AgentSSAS 即开始采集事件并执行检测。

## 第五步：数据检查

运行一段时间后检查落盘数据：

```text
~/.jiuwenswarm/ssas/
├── ssas_core.db                 # 核心库: raw_events / events / alerts 表
├── reports/threat_log/*.json    # OCSF 威胁日志
└── modules/<module>/result.db   # 各检测模块结果
```

```bash
# 查看已采集事件数(需 sqlite3)
sqlite3 ~/.jiuwenswarm/ssas/ssas_core.db "SELECT COUNT(*) FROM events;"
```

更多的数据查看方法参见[查看威胁日志](../how-to/view-threat-logs.md)。

## 常见问题

**启动日志中没有 AgentSSASSecurityRail 相关输出**

- 检查 `~/.jiuwenswarm/config/config.yaml` 是否包含 `ssas` 段且 `enabled: true`——已有用户的配置文件不会被模板覆盖，必须手动添加
- 检查依赖是否安装成功。若日志出现 `AgentSSASSecurityRail not loaded` 或 `agent-ssas not installed`，回到第二步重新执行 `uv sync`

**日志出现 `AgentSSASSecurityRail disabled by config`**

- 说明 `ssas.enabled` 未设为 `true`，修改配置后重启

**HTTP 模式下启动失败或连接被拒**

- 若 `mode: http`，确认独立 SSAS 服务已启动、`http_endpoint` 指向正确地址且端口未被占用；8443 端口被占用时可改用其他端口，参见[HTTP 服务模式](./04-http-mode.md)

**想撤销集成**

```bash
cd jiuwenswarm
git checkout -- jiuwenswarm/resources/config.yaml \
  jiuwenswarm/server/runtime/agent_adapter/interface_deep.py pyproject.toml
```

## 常用调整

| 需求 | 修改 |
| ---- | ---- |
| 关闭 SSAS | `ssas.enabled: false` |
| 切换 HTTP 模式 | `ssas.mode: http` + `ssas.http_endpoint` 指向独立服务（见[HTTP 服务模式](./04-http-mode.md)） |
| 启用主动防护 | `ssas.decision_policy: active_protection`（critical 阻断、其余告警） |
| 启用测试模块 | `ssas.modules.test_detection.enabled: true` |

## 相关资源

- [examples/jiuwenswarm_integration/README.md](../../../examples/jiuwenswarm_integration/README.md)：本教程对应的集成示例说明
- [patches/](../../../patches/)：两个 patch 文件（`jiuwenswarm_ssas_local.patch` / `jiuwenswarm_ssas_online.patch`）
- [配置参考](../reference/configuration.md)：`ssas` 段全部字段说明
