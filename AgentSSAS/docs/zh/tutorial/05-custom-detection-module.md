# 编写自定义检测模块

> 本篇讲解如何为 AgentSSAS 编写一个自定义检测模块：从创建目录、编写 module.yaml，到实现数据建模与威胁分析两类插件，再到加载验证。适合需要在内置模块之外扩展自有检测逻辑的用户。阅读前建议先完成[快速开始](./02-quickstart.md)。

## 检测模块的组成

一个检测模块 = **1 个 module.yaml 声明 + 1 个数据建模插件 + 1 个威胁分析插件**，以独立目录的形式放在：

```text
src/agent_ssas/core/detection_modules/<模块名>/
├── __init__.py          # 包标识
├── module.yaml          # 模块声明(必需)
└── <插件文件>.py         # 模块专属插件(可选,也可复用框架预置插件)
```

两类插件各司其职，经流水线串联：

- **数据建模插件（DataModeler）**：输入事件描述 json（`event_desc`），输出特定格式的建模数据
- **威胁分析插件（ThreatAnalyzer）**：消费建模数据，输出威胁分析报告（含 `has_risk`、`risk_level` 等字段）

`agent_ssas.core.detection_modules` 下的 `test_detection` 是最简参照：它直接复用框架预置的空白插件，把所有事件透传统计上报，用于验证流水线是否跑通。本篇教程也以它为起点。

## module.yaml 字段说明

以 `test_detection` 的声明为例：

```yaml
name: test_detection
display_name: "测试检测模块"
enabled: false
event_version: "1.0"
subscribed_events: ["*"]
analytic_type_id: 1

plugins:
  - type: data_modeling
    name: BlankDataModeler
    model_type: blank
  - type: threat_analysis
    name: BlankThreatAnalyzer
    expected_model_type: blank
```

各字段含义：

| 字段 | 必填 | 默认值 | 说明 |
| ---- | ---- | ------ | ---- |
| `name` | 是 | — | 模块唯一标识，同时决定存储目录 `modules/<name>/` 的名称 |
| `display_name` | 否 | 同 `name` | 展示名称 |
| `enabled` | 否 | `true` | 是否启用，`false` 时加载阶段直接跳过 |
| `event_version` | 否 | `"1.0"` | 事件格式版本 |
| `subscribed_events` | 否 | `["*"]` | 订阅的事件规格列表，写法见[下文](#订阅事件的写法) |
| `analytic_type_id` | 否 | `0` | 分析类型标识 |
| `plugins` | 是 | — | 插件声明列表，必须恰好包含一个 `type: data_modeling` 和一个 `type: threat_analysis` |

`plugins` 列表中每个条目的字段：

| 字段 | 说明 |
| ---- | ---- |
| `type` | `data_modeling`（数据建模）或 `threat_analysis`（威胁分析） |
| `name` | 插件类名。加载时先在框架预置插件目录中查找，找不到再到本模块目录中查找 |
| `model_type` | 建模插件的数据类型标识（仅 data_modeling） |
| `expected_model_type` | 分析插件期望消费的数据类型（仅 threat_analysis），必须与配对建模插件的 `model_type` 一致 |
| `config` | 可选，传给插件构造函数的专有配置 dict |

## 分步教程

下面动手创建一个示例模块 `my_detection`：订阅全部事件，对内容中出现 `rm -rf` 的事件给出高风险报告。

### 第 1 步：创建模块目录

```bash
cd AgentSecurity/AgentSSAS
mkdir src/agent_ssas/core/detection_modules/my_detection
touch src/agent_ssas/core/detection_modules/my_detection/__init__.py
```

> 模块管理器启动时扫描 `detection_modules/` 目录，跳过以 `_` 开头的目录和 `common`（通用组件目录），只加载含 `module.yaml` 的子目录。

### 第 2 步：编写 module.yaml

创建 `src/agent_ssas/core/detection_modules/my_detection/module.yaml`：

```yaml
name: my_detection
display_name: "示例检测模块"
enabled: true
event_version: "1.0"
subscribed_events: ["*"]
analytic_type_id: 1

plugins:
  - type: data_modeling
    name: MyDataModeler
    model_type: my_model
  - type: threat_analysis
    name: MyAnalyzer
    expected_model_type: my_model
```

**建议先跑通再自定义**：如果此时还没有写插件，可以把两个 `name` 暂时改为 `BlankDataModeler` / `BlankThreatAnalyzer`（框架预置的空白插件），先验证模块能被扫描加载，再替换为自有插件。

### 第 3 步：实现插件

插件是实现特定协议的普通 Python 类，不需要继承任何基类：

**数据建模插件接口**：

- 类属性 `name`：插件类名，与 module.yaml 中的 `name` 一致
- 类属性 `model_type`：建模数据类型标识
- 方法 `async def build_model(self, event_desc: dict) -> Any`：输入事件描述 json，返回建模数据

**威胁分析插件接口**：

- 类属性 `name`：插件类名
- 类属性 `expected_model_type`：期望的建模数据类型
- 方法 `async def analyze(self, model_data: Any) -> dict`：输入建模数据，返回威胁分析报告

`build_model` 收到的 `event_desc` 是引擎统一事件序列化后的 json，主要结构：

```text
event_desc
├── event_version     # 事件格式版本
├── event_node        # 事件节点,字段见下
├── aux_ids           # 关联 ID(interaction_seq / session_id / agent_id / trace_id /
│                     #   llm_call_seq / tool_call_seq / tool_call_id)
├── trace             # 追踪结构
└── event_id          # 事件唯一标识(UUID)
```

`event_node` 中常用的字段：

| 字段 | 说明 |
| ---- | ---- |
| `event_type` | 事件类型（如 `tool_input`） |
| `event_class` | 事件大类：`lifecycle` / `security` |
| `node_type` | 节点类型：`session` / `interaction` / `llm_call` / `tool_call` |
| `action_name` | 动作名。工具事件为工具名（如 `bash`），LLM 事件为 `llm_call` |
| `input_content` | 输入内容。`tool_input` 为工具参数，`llm_input` 为 prompt |
| `output_content` | 输出内容。`tool_output` 为工具结果，`llm_output` 为响应 |
| `risk_source` / `risk_type` / `risk_level` / `risk_assessment` | 安全检测事件（`event_class="security"`）携带的风险字段 |

创建 `src/agent_ssas/core/detection_modules/my_detection/my_data_modeler.py`：

```python
from __future__ import annotations

from typing import Any


class MyDataModeler:
    """示例数据建模插件:从事件节点中提取精简的检测视图。"""

    name: str = "MyDataModeler"
    model_type: str = "my_model"

    async def build_model(self, event_desc: dict[str, Any]) -> dict[str, Any]:
        event_node = event_desc.get("event_node", {})
        return {
            "event_type": event_node.get("event_type", ""),
            "event_class": event_node.get("event_class", ""),
            "action_name": event_node.get("action_name", ""),
            "input_content": event_node.get("input_content", ""),
        }
```

创建 `src/agent_ssas/core/detection_modules/my_detection/my_analyzer.py`：

```python
from __future__ import annotations

from typing import Any


class MyAnalyzer:
    """示例威胁分析插件:检测内容中出现 rm -rf 的事件。"""

    name: str = "MyAnalyzer"
    expected_model_type: str = "my_model"

    async def analyze(self, model_data: Any) -> dict[str, Any]:
        if not isinstance(model_data, dict):
            return self._empty_report()

        action_name = model_data.get("action_name", "")
        input_content = str(model_data.get("input_content", ""))
        if "rm -rf" in input_content:
            return {
                "has_risk": True,
                "risk_level": "high",
                "risk_type": "destructive_command",
                "risk_score": 75.0,
                "confidence": 1.0,
                "detected_threats": ["destructive_command"],
                "evidence": {
                    "action_name": action_name,
                    "input_content": input_content,
                },
                "module_name": "my_detection",
            }
        return self._empty_report()

    @staticmethod
    def _empty_report() -> dict[str, Any]:
        return {
            "has_risk": False,
            "risk_level": "safe",
            "risk_type": "",
            "risk_score": 0.0,
            "confidence": 1.0,
            "detected_threats": [],
            "evidence": {},
            "module_name": "my_detection",
        }
```

**插件文件名规则**：模块管理器在模块目录中查找插件时，按"类名转 snake_case"推导文件名——`MyDataModeler` 对应 `my_data_modeler.py`，`MyAnalyzer` 对应 `my_analyzer.py`。文件名不符会导致 `ImportError`。若插件类在框架预置插件目录（`agent_ssas.core.framework.data_modeler.plugins` / `agent_ssas.core.framework.threat_analyzer.plugins`）中已存在（如 `BlankDataModeler`），则无需在模块目录中提供文件。

### 第 4 步：重启加载

检测模块在后端初始化时扫描加载：

- 独立运行（如[快速开始](./02-quickstart.md)的示例）：重新运行脚本即可
- 集成 JiuwenSwarm（inprocess 模式）：重启 `jiuwenswarm-start`
- HTTP 服务模式：重启服务端

加载成功时日志会出现：

```text
检测模块加载完成: name=my_detection, events=['*']
```

单个模块加载失败只会记录异常并跳过，不影响其他模块与主流程（fail-open）。

## 订阅事件的写法

`subscribed_events` 列表支持以下写法：

| 写法 | 含义 |
| ---- | ---- |
| `"*"` | 通配符，订阅所有事件（始终为 notify 模式） |
| `"tool_input"` | 具体事件名，如 6 个生命周期事件或 `permission_interrupt_tool` |

进阶写法（机制预留）：

- `"tool_input:auth"`：加 `:auth` 后缀表示 **auth 模式**——同步检测，上报方等待检测结果返回；不带后缀默认为 **notify 模式**（后台异步检测，不阻断）。当前内置模块均为 notify 模式
- 聚合事件：`one_toolcall_event` / `one_llmcall_event` / `one_interaction_event` 会按引擎内部规则展开为关联的基础事件订阅，auth 模式作用在结束事件上

订阅模式与决策策略的完整说明参见[检测模块参考](../reference/detection-modules.md)与[切换决策策略](../how-to/switch-decision-policy.md)。

## 传入模块专属配置

插件专有配置通过 module.yaml 的 `plugins[].config` 传入，加载时优先以该 dict 作为参数调用插件构造函数；构造函数不接受参数时回退为无参实例化。内置模块 `agent_moss` 就是这种用法：

```yaml
plugins:
  - type: threat_analysis
    name: AgentMossAnalyzer
    expected_model_type: agent_behavior_model
    config:
      analysis_methods: [rule, behavior_chain, pdg]
      risk_threshold: low
      max_history_events: 200
```

对应地，插件可以在构造函数中接收并保存配置：

```python
class MyAnalyzer:
    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self.keyword = cfg.get("keyword", "rm -rf")
```

在 jiuwenswarm 侧，也可以用 `~/.jiuwenswarm/config/config.yaml` 的 `ssas.modules.<模块名>` 段覆盖模块行为，当前支持覆盖 `enabled` 开关（例如临时启用 `test_detection`）：

```yaml
ssas:
  modules:
    test_detection:
      enabled: true
```

> `ssas.modules` 段的其余插件专有配置为预留扩展，当前版本以 module.yaml 中的声明为准。配置项的完整说明参见[配置参考](../reference/configuration.md)。

## 验证

以[快速开始](./02-quickstart.md)的示例为例，`uv run python examples/inprocess_quickstart/main.py` 上报的 7 个事件会流经 `my_detection`，其中 `permission_interrupt_tool` 事件的工具参数包含 `rm -rf /`，会被示例分析插件判为 `high`。验证方法：

1. 检查 `~/.jiuwenswarm/ssas/modules/my_detection/result.db` 是否出现该模块的检测结果记录（该目录在模块首次加载后自动创建）
2. 检查 `~/.jiuwenswarm/ssas/reports/threat_log/` 下是否出现文件名含 `my_detection` 的 OCSF 威胁日志
3. 加载日志确认 `检测模块加载完成: name=my_detection, events=['*']`

注意：由于当前为 notify 模式，`report_event` 的返回值仍是聚合后的即时结果（通常为 `safe`），高风险结论异步写入 `result.db` 与威胁日志，不会阻断主流程。

## 相关资源

- [src/agent_ssas/core/detection_modules/test_detection/](../../../src/agent_ssas/core/detection_modules/test_detection/)：最简模块参照
- [src/agent_ssas/core/detection_modules/agent_moss/module.yaml](../../../src/agent_ssas/core/detection_modules/agent_moss/module.yaml)：带完整插件配置的复杂模块示例
- [事件格式参考](../reference/event-format.md)：事件三层结构与字段定义
- [检测模块参考](../reference/detection-modules.md)：内置模块与 module.yaml 的完整字段说明
- [架构解析](../explanation/architecture.md)：检测模块在引擎中的位置
