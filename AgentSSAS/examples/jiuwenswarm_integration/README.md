# JiuwenSwarm 集成示例（jiuwenswarm_integration）

本示例说明如何将 AgentSSAS 接入 JiuwenSwarm：通过应用 patch，`AgentSSASSecurityRail` 会被注册到 JiuwenSwarm 的 DeepAgent，自动采集 Agent 行为事件并执行安全威胁检测。

patch 修改 jiuwenswarm 的 3 个文件：`config.yaml`（新增 ssas 配置段）、`interface_deep.py`（注册 AgentSSASSecurityRail）、`pyproject.toml`（声明 agent-ssas 依赖）。

## 前置条件

- Python >= 3.11，已安装 [uv](https://docs.astral.sh/uv/)
- **本地依赖版**：`jiuwenswarm` 与 `AgentSecurity` 为同级目录：

  ```text
  <workspace>/
  ├── jiuwenswarm/          # JiuwenSwarm 仓库(develop 分支)
  └── AgentSecurity/
      └── AgentSSAS/        # 本仓库
  ```

- **在线依赖版**：可访问 `https://atomgit.com/yieux1/AgentSecurity`

## 第一步：应用 patch（二选一）

在 jiuwenswarm 仓库根目录执行：

```bash
# 方式 A: 本地依赖版 —— editable 引用本地 AgentSSAS 源码,改源码立即生效,适合开发调试
git apply ../AgentSecurity/AgentSSAS/patches/jiuwenswarm_ssas_local.patch

# 方式 B: 在线依赖版 —— 从 atomgit 拉取 agent-ssas,适合正式使用
git apply ../AgentSecurity/AgentSSAS/patches/jiuwenswarm_ssas_online.patch
```

> 提示：在线依赖版不要求两仓同级存放，任意路径的 jiuwenswarm 仓库都可应用。

## 第二步：安装依赖

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
- **已有用户**：`~/.jiuwenswarm/config/config.yaml` 已存在（不会被模板覆盖），请手动把 `ssas` 段加入该文件

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

## 常用调整

| 需求 | 修改 |
| ---- | ---- |
| 关闭 SSAS | `ssas.enabled: false` |
| 切换 HTTP 模式 | `ssas.mode: http` + `ssas.http_endpoint` 指向独立服务（见 [http_server_demo](../http_server_demo/)） |
| 启用主动防护 | `ssas.decision_policy: active_protection`（critical 阻断、其余告警） |
| 启用测试模块 | `ssas.modules.test_detection.enabled: true` |

## 回滚

```bash
cd jiuwenswarm
git checkout -- jiuwenswarm/resources/config.yaml \
  jiuwenswarm/server/runtime/agent_adapter/interface_deep.py pyproject.toml
```
