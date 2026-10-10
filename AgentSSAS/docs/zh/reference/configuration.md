# 配置参考

AgentSSAS 的配置由 `AgentSSASConfig`（定义于 `src/agent_ssas/core/framework/config/settings.py`）承载，从 JiuwenSwarm 的 `config.yaml` 的 `ssas` 段加载，并可被 `SSAS_` 前缀的环境变量覆盖。配置注入到各模块（接入适配、数据预处理、流水线、数据建模、威胁分析、存储、呈现、检测模块管理器）。

相关文档：

- [切换决策策略](../how-to/switch-decision-policy.md)：`decision_policy` 的使用方法
- [HTTP API 参考](./http-api.md)：HTTP 模式相关配置的运行效果
- [检测模块参考](./detection-modules.md)：`modules` 段与 module.yaml 的关系

## 配置加载顺序与优先级

加载顺序为：

1. **默认值**：`AgentSSASConfig` 各字段的 dataclass 默认值。
2. **config.yaml 的 `ssas` 段**：`AgentSSASConfig.from_dict(data)` 读取 `ssas` 段内容，仅识别已知字段（未知字段被忽略）；`from_dict(None)` 时直接使用默认值。`AgentSSASConfig.from_yaml(path)` 读取整个 config.yaml 并提取 `ssas` 段；文件中没有 `ssas` 段时等同默认配置。
3. **`SSAS_` 前缀环境变量**：在上述结果上覆盖，优先级最高。

即：`SSAS_` 环境变量 > config.yaml `ssas` 段 > 字段默认值。

环境变量的解析规则：

- 布尔字段：值不区分大小写，`true` 视为真，其余视为假。
- 整数/浮点字段：解析失败时忽略该环境变量（保留原值）。
- `SSAS_MODE`：转换为 `AgentSSASMode` 枚举（`inprocess` / `http`），非法值忽略。
- `SSAS_HOME`：既映射到 `ssas_home` 字段，也参与存储根目录解析（见下文）。

## AgentSSASConfig 全量字段

| 字段 | 类型 | 默认值 | 环境变量 | 说明 |
|------|------|--------|---------|------|
| `enabled` | bool | `true` | `SSAS_ENABLED` | 全局开关 |
| `mode` | str（`inprocess` / `http`） | `inprocess` | `SSAS_MODE` | 运行模式 |
| `http_endpoint` | str | `http://localhost:8443` | `SSAS_HTTP_ENDPOINT` | HTTP 模式服务端地址（客户端用） |
| `http_timeout` | float | `5.0` | `SSAS_HTTP_TIMEOUT` | HTTP 客户端超时秒数 |
| `http_token` | str / None | `None` | `SSAS_HTTP_TOKEN` | Bearer token 认证；非 None 时启用 |
| `http_host` | str | `0.0.0.0` | `SSAS_HTTP_HOST` | HTTP 服务端监听地址 |
| `http_port` | int | `8443` | `SSAS_HTTP_PORT` | HTTP 服务端端口 |
| `ssas_home` | str / None | `None` | `SSAS_HOME` | 存储根目录；None 时按环境变量链解析 |
| `storage_backend` | str | `sqlite` | — | 存储后端（预留字段，当前版本未生效） |
| `event_ttl_days` | int | `30` | `SSAS_EVENT_TTL_DAYS` | 事件保留天数（清理语义见下文） |
| `alert_ttl_days` | int | `90` | `SSAS_ALERT_TTL_DAYS` | 告警保留天数（清理语义见下文） |
| `rail_priority` | int | `80` | `SSAS_RAIL_PRIORITY` | Rail 执行优先级（预留字段，当前版本未生效） |
| `enable_exception_hooks` | bool | `true` | `SSAS_ENABLE_EXCEPTION_HOOKS` | 异常钩子开关（预留字段，当前版本未生效） |
| `risk_report_threshold` | str | `low` | `SSAS_RISK_REPORT_THRESHOLD` | 风险上报门限（safe/low/medium/high/critical） |
| `auth_timeout` | float | `2.0` | `SSAS_AUTH_TIMEOUT` | auth 模式订阅者超时秒数 |
| `decision_policy` | str | `observe_only` | `SSAS_DECISION_POLICY` | 决策策略模式名 |
| `modules` | dict | `{}` | — | 各检测模块专属配置 |

说明：

- `mode` 对应 `AgentSSASMode` 枚举：`inprocess`（进程内库模式，零网络延迟）与 `http`（独立 HTTP 服务模式，支持多 Agent 共享）。
- `risk_report_threshold` 取值为五级风险等级之一（`safe` / `low` / `medium` / `high` / `critical`），低于门限的风险不上报。
- `auth_timeout`：auth 模式订阅者超过此时间未返回时，按模块默认策略处理。
- `modules` 无对应环境变量，只能通过 config.yaml 配置。
- `storage_backend`、`rail_priority`、`enable_exception_hooks`、`risk_report_threshold` 为预留字段，当前版本未生效。

## TTL 数据清理机制

`event_ttl_days` 与 `alert_ttl_days` 控制过期数据的自动清理，清理映射为：

- **主库 ssas_core.db**：`events`、`raw_events` 表按 `event_ttl_days` 清理，`alerts` 表按 `alert_ttl_days` 清理；
- **模块库**：每个模块的 `process.db` 按 `event_ttl_days` 清理，`result.db` 按 `alert_ttl_days` 清理。

触发时机有两处：

1. `AgentSSASBackend.initialize()` 启动时执行一次（清理失败不影响初始化）；
2. 运行期每累计 100 次事件上报，后台异步触发一次（不阻塞上报路径）。

`event_ttl_days` / `alert_ttl_days` 设为 `<= 0` 时，禁用对应表的清理（数据永久保留）。

## config.yaml `ssas` 段完整示例

```yaml
ssas:
  # 全局开关
  enabled: true
  # 运行模式: inprocess(默认) / http
  mode: inprocess

  # 存储配置
  ssas_home:                  # 留空时按 SSAS_HOME > JIUWENSWARM_DATA_DIR > JIUWENSWARM_HOME > ~/.jiuwenswarm 解析
  storage_backend: sqlite
  event_ttl_days: 30
  alert_ttl_days: 90

  # Rail 配置
  rail_priority: 80
  enable_exception_hooks: true
  risk_report_threshold: low

  # 决策策略: observe_only(默认) / active_protection / 自定义策略名
  decision_policy: observe_only

  # auth 模式订阅者超时秒数
  auth_timeout: 2.0

  # HTTP 模式专用(mode=http 时生效)
  http_endpoint: "http://localhost:8443"
  http_timeout: 5.0
  http_token:                  # 留空(Null)不启用认证; 填写后启用 Bearer token 认证
  http_host: "0.0.0.0"
  http_port: 8443

  # 各检测模块专属配置
  modules:
    test_detection:
      enabled: true
```

注意：JiuwenSwarm 用户 config.yaml 默认不包含 `ssas` 段，必须显式添加并设置 `enabled: true` 才能启用 SSAS。

## 存储路径解析

存储根目录（`ssas_home`）的解析优先级：

```text
SSAS_HOME > JIUWENSWARM_DATA_DIR > JIUWENSWARM_HOME > ~/.jiuwenswarm
```

- `AgentSSASConfig.ssas_home` 字段（可由 config.yaml 或 `SSAS_HOME` 环境变量设置）非空时直接采用。
- 字段为 None 时，按上述环境变量链取第一个非空值。
- 全部未设置时回退到用户主目录下的 `~/.jiuwenswarm`。

最终存储路径规则：`storage_path = <ssas_home>/ssas`，即：

```text
<ssas_home>/
└── ssas/                        # storage_path
    ├── ssas_core.db             # 核心库(raw_events 等表)
    ├── modules/<模块名>/         # 各检测模块独立存储
    │   ├── process.db
    │   ├── result.db
    │   └── config/
    └── reports/threat_log/      # OCSF 威胁日志
```

## modules 段结构

`modules` 是以检测模块名为键的字典，用于为各模块提供专属配置。当前版本支持覆盖模块的启用开关：

```yaml
ssas:
  modules:
    agent_moss:
      enabled: true        # 覆盖 module.yaml 的 enabled 默认值
    security_rail_detection:
      enabled: true
    test_detection:
      enabled: false
```

规则：

- 键名必须与检测模块 `module.yaml` 的 `name` 字段完全一致，拼写错误不会报错，但启动日志会输出警告（`config.yaml 中配置的模块名 '<name>' 未找到对应的检测模块`）。
- 每个模块的值为字典，当前识别 `enabled` 字段；`enabled: false` 的模块在启动扫描时被跳过（不加载插件、不创建存储、不注册订阅）。
- 该段为模块运行期配置的预留扩展点，后续版本可承载更多模块级参数。

模块本身的声明（订阅事件、插件、分析参数等）在 module.yaml 中定义，详见[检测模块参考](./detection-modules.md)；启用/禁用的操作步骤详见[启用与禁用检测模块](../how-to/enable-disable-detection-modules.md)。

## 环境变量速查

```bash
SSAS_ENABLED=true
SSAS_MODE=inprocess
SSAS_HTTP_ENDPOINT=http://localhost:8443
SSAS_HTTP_TIMEOUT=5.0
SSAS_HTTP_TOKEN=<token>
SSAS_HTTP_HOST=0.0.0.0
SSAS_HTTP_PORT=8443
SSAS_HOME=/path/to/home
SSAS_EVENT_TTL_DAYS=30
SSAS_ALERT_TTL_DAYS=90
SSAS_RAIL_PRIORITY=80
SSAS_ENABLE_EXCEPTION_HOOKS=true
SSAS_RISK_REPORT_THRESHOLD=low
SSAS_AUTH_TIMEOUT=2.0
SSAS_DECISION_POLICY=observe_only
```

另有三个仅参与存储根目录解析链的环境变量：`JIUWENSWARM_DATA_DIR` 与 `JIUWENSWARM_HOME`（不带 `SSAS_` 前缀），以及链首的 `SSAS_HOME`（带 `SSAS_` 前缀，同时映射 `ssas_home` 字段）。

## 相关文档

- [事件格式参考](./event-format.md)
- [检测模块参考](./detection-modules.md)
- [HTTP API 参考](./http-api.md)
- 仓库根 [README.md](../../../README.md)
