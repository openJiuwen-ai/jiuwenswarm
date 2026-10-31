# 启用与禁用检测模块

AgentSSASCore 在启动时扫描检测模块目录并加载所有已启用的模块。本文说明如何通过配置覆盖或直接修改 `module.yaml` 来启用、禁用检测模块，以及如何在运行后验证结果。

前置阅读：

- [检测模块参考](../reference/detection-modules.md)：模块声明文件的完整字段说明
- [配置参考](../reference/configuration.md)：`ssas.modules` 段的结构

## 内置模块默认状态

| 模块名 | 默认状态 | 订阅事件 |
|--------|---------|---------|
| `agent_moss` | 启用 | 6 个生命周期事件 |
| `security_rail_detection` | 启用 | `permission_interrupt_tool` |
| `test_detection` | 禁用 | `*`（全部事件） |

## 方式一：通过 config.yaml 覆盖（推荐）

在 JiuwenSwarm 的 `config.yaml` 的 `ssas` 段中，通过 `modules.<模块名>.enabled` 覆盖 `module.yaml` 中的默认值，无需改动源码：

```yaml
ssas:
  enabled: true
  mode: inprocess
  modules:
    # 启用默认关闭的测试模块
    test_detection:
      enabled: true
    # 禁用默认启用的 agent_moss
    agent_moss:
      enabled: false
```

覆盖规则：

- 仅当模块名与已加载模块的 `name` 字段匹配时生效，且当前仅支持覆盖 `enabled` 字段。
- 如果 `config.yaml` 中配置的模块名没有对应的检测模块（例如拼写错误），启动日志会出现警告：`config.yaml 中配置的模块名 '<name>' 未找到对应的检测模块，请检查拼写是否正确`。
- 该方式不修改包内文件，升级、重装后仍然有效，是运行期调整模块开关的推荐方式。

## 方式二：直接修改 module.yaml（源码方式）

每个检测模块的声明文件位于包内：

```text
src/agent_ssas/core/detection_modules/<模块名>/module.yaml
```

直接编辑其中的 `enabled` 字段。以启用 `test_detection` 为例，修改 `src/agent_ssas/core/detection_modules/test_detection/module.yaml`：

```yaml
name: test_detection
display_name: "测试检测模块"
enabled: true
event_version: "1.0"
subscribed_events: ["*"]
```

注意事项：

- 该方式修改的是安装包内的文件。editable 安装（`uv pip install -e .`）下修改源码目录即生效；正式安装（非 editable）下修改的是 site-packages 中的副本，重装或升级会被覆盖。
- 适合开发者调试自定义模块时使用；日常运维建议使用方式一。

## 重启生效

检测模块在 `DetectionModuleManager` 初始化（即 SSAS 后端启动）时一次性扫描、加载并构建订阅表，运行期间不会重新扫描。因此无论采用哪种方式，修改后都需要重启 JiuwenSwarm（inprocess 模式）或 SSAS HTTP 服务（http 模式）才能生效。

## 验证

### 查看启动日志

模块加载结果会写入日志：

- 加载成功：`检测模块加载完成: name=<模块名>, events=[...]`
- 未启用跳过：`检测模块未启用,跳过: <模块名>`

启用 `test_detection` 后，启动日志中应能看到 `name=test_detection` 的加载记录；禁用 `agent_moss` 后，则应看到 `检测模块未启用,跳过: agent_moss`。

### 检查存储目录

每个成功加载的模块会在存储根目录下创建独立子目录（含过程库、结果库与配置目录）：

```text
<storage>/modules/<模块名>/
├── process.db    # 过程数据（建模数据、中间结果）
├── result.db     # 结果数据（告警、审计、威胁分析报告）
└── config/       # 配置数据（规则、基线、阈值参数）
```

其中 `<storage>` 默认为 `~/.jiuwenswarm/ssas/`，可通过 `SSAS_HOME` 等环境变量调整，详见[配置参考](../reference/configuration.md)。

模块启用后，首次事件处理会触发目录与数据库文件的创建。如果 `modules/<模块名>/` 目录未生成，说明该模块未被加载（未启用或加载失败，加载失败时日志会记录异常堆栈）。

## 相关文档

- [切换决策策略](./switch-decision-policy.md)
- [查看威胁日志](./view-threat-logs.md)
- [检测模块参考](../reference/detection-modules.md)
