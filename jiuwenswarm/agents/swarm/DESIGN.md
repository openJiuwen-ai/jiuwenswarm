# Swarm Spec 装配设计文档

> 模块路径：`jiuwenswarm/agents/swarm/`
> 范围：team 成员（leader / teammate）的 harness（rail / tool / sub-agent）**纯声明式装配**——从 config 源派生 spec，经 openjiuwen 构造为运行对象，并提供可序列化的自描述 manifest。

---

## 0. 一句话总览

一个 team 成员的能力 = 一组**声明式 spec**（`RailSpec` / `BuiltinToolSpec` / `SubAgentSpec`）→ openjiuwen 按 `type` / `factory_name` 解析到 **swarm provider 工厂** → 工厂用 **ConstructionInput** 从 `params`（属性）+ `SwarmBuildContext`（环境）提取构造参数 → 产出运行对象。所有元素另有一份**可序列化 descriptor**（manifest catalog），用于注册、内省与未来配置文件装配。

> **框架分层**：manifest 这套「descriptor + catalog + ConstructionInput + 反射 + catalog 驱动注册」是 harness 通用基础设施，已**下沉到 openjiuwen**（`openjiuwen.agent_teams.harness.manifest`）。swarm 只保留：① 各 provider 的元素**声明**（`@harness_element`）；② `SwarmBuildContext` 与 `config_specs` 烘焙；③ 对框架的**注册编排**（`registry.py` 调 `register_from_catalog` + `register_build_context_factory`）。

设计三原则（见 `memory`）：
1. **纯声明式装配**：能力声明为 config 源 provider spec，禁止 customizer / `rail.init` 后处理挂载。
2. **成员共享 config 源**：不从预构造父 DeepAgent 二次继承；创建 team 不必先造单 agent。
3. **跨序列化边界靠 seed 重建**：成员 spawn / 分布式 / 热恢复经 `build_context_seed` + 注册工厂重建上下文。

---

## 1. 分层架构

```
请求 (mode, role, channel, session, project_dir, config.yaml)
        │
        ▼
 enrich_team_spec_for_swarm()                         assembly.py
        │  ① register_swarm_providers()  ② 建 SwarmBuildContext  ③ 改写成员 spec  ④ 挂 build_context + seed
        ▼
 TeamAgentSpec                                         (openjiuwen schema)
   ├─ agents["leader"]   : DeepAgentSpec
   └─ agents["teammate"] : DeepAgentSpec
            ├─ rails    : list[RailSpec]               config_specs.py 折叠 + 属性烘焙进 params
            ├─ tools    : list[BuiltinToolSpec]
            └─ subagents: list[SubAgentSpec]
        │
        ▼  spec.build(context=SwarmBuildContext)       openjiuwen 解析 type/factory_name
 swarm provider 工厂  build_xxx(params, ctx)            providers/*.py
        │  inp = XxxInput.resolve(params, ctx)         openjiuwen harness/manifest
        ▼
 运行对象 (Rail / Tool / SubAgentConfig)
```

四个职责面：

| 面 | 文件 | 职责 |
|---|---|---|
| **声明 + 装配** | `assembly.py`、`config_specs.py` | 从 config 派生成员 spec；把 config 派生**属性烘焙进 `params`** |
| **构造** | `providers/*.py` + openjiuwen `harness/manifest`（框架） | provider 工厂声明 + `ConstructionInput` 从 params+context 提取并构造对象 |
| **运行载体** | `context.py` | `SwarmBuildContext`：构造期的环境句柄 |
| **自描述** | openjiuwen `harness/manifest`（框架）+ `providers/*.py` 声明 + `registry.py` 注册编排 | 可序列化 descriptor + catalog 驱动注册 + 内省 |

---

## 2. openjiuwen Spec 与注册底座（依赖，不在本模块）

安装位置：openjiuwen `agent_teams/schema/`（框架依赖，不在本模块仓库内）。

### 2.1 Spec 模型（pydantic，可 JSON round-trip）

| Spec | 关键字段 | `.build()` 解析 |
|---|---|---|
| `RailSpec` | `type: str`、`params: dict` | 只走 provider（`_RAIL_PROVIDER_REGISTRY[type]`），未命中抛 `ValueError`（openjiuwen 已删 class 注册表） |
| `BuiltinToolSpec` | `type: str`、`params: dict` | 同上（`_TOOL_PROVIDER_REGISTRY`） |
| `SubAgentSpec` | `agent_card`、`system_prompt`、`factory_name`、`factory_kwargs: dict`、… | `factory_name` 命中 `_SUBAGENT_PROVIDER_REGISTRY` 时走 provider |
| `DeepAgentSpec` | `rails` / `tools` / `subagents` / `system_prompt` / `model` / … | 逐元素 `.build()` 装配成 `DeepAgent` |
| `TeamAgentSpec` | `agents: dict[role, DeepAgentSpec]`、`team_name`、`build_context`、`build_context_seed` | team 机制构造各成员 |

### 2.2 注册表（纯 `dict[str, Callable]`，**无元数据**）

```python
_RAIL_PROVIDER_REGISTRY: dict[str, Callable]      # register_rail_provider(name, factory)
_TOOL_PROVIDER_REGISTRY: dict[str, Callable]      # register_tool_provider(name, factory)
_SUBAGENT_PROVIDER_REGISTRY: dict[str, Callable]  # register_subagent_provider(name, factory)
```

工厂签名约定：
- rail / tool：`factory(params: dict, context: BuildContext) -> obj | list | None`
- subagent：`factory(factory_kwargs: dict, context: BuildContext) -> SubAgentConfig | None`

### 2.3 BuildContext 与 seed

`BuildContext` 是 **dataclass（非 pydantic）**——Spec→Runtime 边界对象，**不参与 JSON 序列化**。跨序列化边界（spawn / 分布式 / 冷恢复）通过：
- `register_build_context_factory(factory)`：平台注册「从 seed 重建 context」工厂；
- `TeamAgentSpec.build_context_seed`：spec 上携带可序列化 seed；
- 接收侧 `build_context_from_seed(seed)` 用本地句柄（config / trajectory span processor）重建活 context。

---

## 3. SwarmBuildContext（环境载体）

`context.py` 的 `SwarmBuildContext(BuildContext)`，是构造期所有 provider 工厂拿到的第二个入参。**只承载 per-request / per-session / per-member 的运行时句柄**（环境值），不承载 harness 设定（那是 params）。

| 字段 | 类型 | 来源 | 进 seed？ |
|---|---|---|---|
| `session_id` / `request_id` / `channel_id` / `channel` / `request_metadata` | str / dict | 请求 | ✅（除非句柄） |
| `mode` | str | 请求（team / code.team / team.plan） | ✅ |
| `project_dir` | str\|None | 请求 | ✅ |
| `team_id` / `team_ws_root` / `team_skill_visibility_path` / `global_skills_dir` | str | team 工作区 | ✅ |
| `config` | dict | `get_config()`（config.yaml） | ❌（接收侧本地 `get_config()`） |
| `trajectory_span_processor` | Any | 进程级 trajectory span processor | ❌（接收侧本地注入） |
| `language` / `member_name` / `role` / `workspace` / `member_card_id` | — | openjiuwen `setup_agent` 经 `derive()` 注入 | per-member 派生 |
| `extras` | dict | 进程内 side-channel | 运行时句柄（如 `_parent_model` / `_coding_memory_rail`） |

- `to_seed()` / `from_seed(seed, *, config, trajectory_span_processor)`：序列化只导出原语，`config` / `trajectory_span_processor` 由接收侧本地注入。
- **`config` 例外说明**：`config` 虽挂在 context 上，但它是 *harness 设定的源*（属性），不是 per-request 环境值。本模块刻意**不让 provider 工厂直接读 `ctx.config` 派生构造参数**——那些属性由 `config_specs` 烘焙进 `params`（见 §6、§7）。`ctx.config` 仅保留给基础设施级用途（如 evolution 热重载 `bind_swarm_context(config=ctx.config)`）。

---

## 4. providers/：能力工厂

`providers/` 下按域分文件，每个文件声明若干 `swarm.*` 名称常量 + `build_*` 工厂 + `@harness_element` 装饰；类 rail 集中在 `providers/builtin_rails.py`。

| 文件 | 元素 |
|---|---|
| `tools.py` | base_tools、code_extra_tools |
| `runtime_tools.py` | cron_tools、send_file |
| `skills.py` | member_skill_toolkit |
| `member_rails.py` | runtime_prompt、team_workspace_report_path、context_processor、plugin_rails |
| `evolution_rails.py` | team_skill_evolution、team_skill_create、member_skill_evolution |
| `code_rails.py` | 10 个 `swarm.code_*` rail（lsp / confirm_interrupt / worktree 已下沉 openjiuwen） |
| `code_subagents.py` | code agent（explore / plan / browser 已下沉 openjiuwen） |
| `builtin_rails.py` | 3 个 swarm 自有无参类 rail（response_prompt / stream_event / avatar_prompt） |

工厂体范式（保留 `(params, ctx)` 签名，openjiuwen 契约不变）：

```python
@harness_element(kind=ElementKind.RAIL, name=STRUCTURED_ASK_USER, description="...",
                 input_model=StructuredAskUserInput)
def build_structured_ask_user(params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    inp = StructuredAskUserInput.resolve(params, ctx)  # 从 params+context 提取并校验
    return StructuredAskUserRail(language=inp.language)
```

---

## 5. ConstructionInput（构造输入框架）

框架在 `openjiuwen.agent_teams.harness.manifest`（`inputs` 子模块）。每个元素在 swarm `providers/*.py` 声明一份 `ConstructionInput` 子类，**逐字段标注来源**，`resolve` 在构造期从 `params` + `context` 提取并 pydantic 校验。

```python
class InputSource(str, Enum):
    PARAMS = "params"     # harness 设定属性（config_specs 烘焙进 RailSpec.params）
    CONTEXT = "context"   # per-request 运行时环境（SwarmBuildContext 字段 / 派生）

def param_field(*, default=..., default_factory=None, description=""): ...
def context_field(*, attr=None, resolver=None, default=None, description=""): ...

class ConstructionInput(BaseModel):
    @classmethod
    def resolve(cls, params, context) -> Self:
        # 逐字段：source=params → params[name]（缺省走默认）
        #         source=context → getattr(context, attr) 或 resolver(context)
        # None 结果丢弃以走字段默认，保留工厂对缺省句柄的容忍
```

- **来源元数据可序列化**：`context_field` 把 `source` + `context_attr` / `resolver_ref`（entry-point 点路径）写进 `Field(json_schema_extra=...)`；`resolver_ref` 由 `factory_ref(resolver)` 计算，可经 `resolve_factory` 反射还原 → 整个 `input_schema` 跨进程可述。
- **不进 schema 的输入**：`ctx.extras` 运行时句柄（parent_model / coding_memory）、全局（skill 目录 / 禁用技能 / rail manager）、env（API key / BROWSER_DRIVER）——属运行时 / 部署 plumbing，工厂直读，不建模。

---

## 6. param vs context 边界（核心心智模型）

> 这是本模块最重要的概念约定。

| 类别 | 判据 | 去向 | 谁填 |
|---|---|---|---|
| **属性值** | 是 config.yaml 里的 harness 设定吗？换请求不变 | `params`（`param_field`） | `config_specs` 在 spec-build 期烘焙 |
| **环境值** | 随请求 / 会话 / 成员动态变化吗？ | `context`（`context_field`） | 运行时由 `SwarmBuildContext` 注入 |
| **基础设施/句柄** | 既非设定也非构造参数（extras / 全局 / env） | 不建模 | 工厂直读 ctx / 全局 / env |

**关键洞察**：`ctx.config` 是「伪装成环境的属性源」。所有 config 派生的构造参数（`skill_mode` / `model_name` / `auto_scan` / `permissions` / `embed` 等）**本质是属性**，统一由 `config_specs` 读 config → 投射进 `RailSpec.params`（与既有 `skills` / `tool_names` / `max_iterations` 同一套机制），provider 工厂只从 `params` 读，**不再在构造期读 `ctx.config` 派生**。

收益：`RailSpec.params` 自洽且可序列化地描述元素的**全部设定**；`SwarmBuildContext` 保持纯环境；为「配置文件 → harness」loader 打好基础（文件即 params）。

---

## 7. config_specs.py：声明 + 属性烘焙

### 7.1 模式 / 角色 → 元素集合

```python
_CODE_MODES = {"code.team", "team.plan"}
_COMMON_RAIL_NAMES / _COMMON_TOOL_NAMES        # team 档
_CODE_RAIL_NAMES / _CODE_SHARED_RAIL_NAMES / _CODE_TOOL_NAMES   # code 档
_role_evolution_rails(config, role)            # leader: 进化+创建；teammate: 成员进化
```

`build_member_capability_specs(config, mode, role) -> (rails, tools)` 按 mode 分档；`build_member_subagent_specs` 仅 code 档（explore/plan 常驻，code/browser 受 `react.subagents.<name>.enabled` 门控）；`build_member_deep_agent_spec` 把能力 spec 折叠到 base `DeepAgentSpec`。

### 7.2 属性烘焙（本模块核心机制）

参数化元素显式构造 `RailSpec(type=name, params=...)`；其余经分派注入：

```python
_RAIL_PARAM_BUILDERS: dict[name, (config) -> params]   # context_processor / code_project_memory /
                                                       # permission_interrupt / code_coding_memory /
                                                       # user_hooks
_TOOL_PARAM_BUILDERS: dict[name, (config) -> params]   # send_file / code_extra_tools

[RailSpec(type=name, params=_rail_params(name, config)) for name in _CODE_RAIL_NAMES]
```

`_extract_*` helper 复用既有底层函数（不重写逻辑）：

| param 字段（元素） | 抽取来源 |
|---|---|
| `skill_mode`（core.team.skill_use） | `react.skill_mode` 校验（`SkillUseRail.SKILL_MODE_*`）；开启 agentic retrieval 时强制 `auto_list` |
| `additional_directories`（code_project_memory） | `react.project_memory` + env `JIUWENSWARM_ADDITIONAL_DIRECTORIES` |
| `permissions_config` + `model_name`（permission_interrupt） | `config.permissions` + `config.models.default…model_name` |
| `embed_config`（code_coding_memory） | `config.embed` |
| `hooks_section`（user_hooks） | `config.hooks` |
| `context_engine_enabled` + `context_engine_config`（context_processor） | `get_context_engine_enabled(config)` + `config.context_engine_config` |
| `acp_enabled`（code_extra_tools） | `config.acp_agents` 非空 |
| `channels_config`（send_file） | `config.channels` |
| `evolution_model_config` + `auto_save`（evolution×2） | `resolve_model_config(config)`（序列化 dict）+ `react.evolution.auto_save` |
| `skill_evolution`（evolution rails） | `react.evolution.skill_evolution` |

> **evolution_llm**：`config_specs` 只烘焙*可序列化的模型配置*（`model_client_config` / `model_config_obj` / `model_name`）进 params；活的 LLM 句柄由工厂 `_build_evolution_llm_from(inp.evolution_model_config)` 在 **build 期**构造，**不进 schema**。

> **blob builder**（permission / coding_memory / context_processor）签名不动：`config_specs` 抽 config 子树进 params，工厂传子 dict（如 `build_permission_rail(config={"permissions": inp.permissions_config}, ...)`）。**零 legacy 回归面**。

> **配置烘焙**：canonical `react.evolution` 派生位在 enrich 期一并解析烘焙，随 spec 序列化——与既有 params 一致（团队级配置）。

---

## 8. assembly.py：enrich 流程

`enrich_team_spec_for_swarm(spec, *, session_id, mode, project_dir, request_id, channel_id, request_metadata)`（就地改写 `spec`）：

1. `register_swarm_providers()`（幂等，把 manifest catalog 驱动注册进 openjiuwen 注册表）；
2. 用 `get_config()` + 工作区路径 + 进程级 trajectory span processor 建 `SwarmBuildContext`；
3. 对 `leader` / `teammate` 调 `build_member_deep_agent_spec`，把能力 spec（含烘焙好的 params）折叠到成员 `DeepAgentSpec`；
4. `spec.build_context = base`；`spec.build_context_seed = base.to_seed()`（跨边界重建）。

---

## 9. manifest：自描述 descriptor catalog（框架在 openjiuwen）

> 本节描述的 descriptor / catalog / 反射 / 注册驱动均为通用框架，位于 `openjiuwen.agent_teams.harness.manifest`。swarm 通过 `@harness_element` 声明元素、`registry.py` 驱动注册，不持有框架实现。

### 9.1 descriptor 模型（`harness/manifest/models.py`）

```python
class HarnessElementDescriptor(BaseModel):
    kind: ElementKind            # tool / rail / subagent
    name: str                    # "swarm.*"，即 spec 的 type / factory_name
    description: str
    factory_ref: str             # "module:qualname"，反射点路径
    input_schema: dict           # ConstructionInput.model_json_schema()，每属性带 source 标记
    input_model_ref: str | None  # 入参模型点路径，供未来 loader 校验
    interface_methods: list[InterfaceMethod]   # 构造对象对外接口方法 + 描述
```

`name` / `description` / `input_schema` 即「LLM tool 三件套」镜像（对照 `ToolCard`），扩展 `kind` / `factory_ref` / `interface_methods` 适配 harness。

### 9.2 单一声明入口 + catalog 驱动注册

```python
@harness_element(kind=..., name=..., description=..., input_model=...)   # 装饰工厂
harness_element(kind=RAIL, name=..., builder=SomeRailClass)              # 直接声明类 rail
```

- `harness_element` **纯元数据记录器**：算 `factory_ref`、取 `input_model.model_json_schema()`、按 kind 兜底 `interface_methods`，写入进程内 `_CATALOG`；**不做 openjiuwen 注册**。
- `register_from_catalog()`（被 `registry.register_swarm_providers` 调用）遍历 catalog，按 kind 分派到 `register_tool/rail/subagent_provider`。
- **rail 归一**：descriptor 层只有 `RAIL`（无 `RAIL_TYPE`）。类 rail 经 `class_rail_adapter(cls)` 包成 `(params, context) -> cls`（复刻 openjiuwen class 分支的 language 注入），统一走 `register_rail_provider`——彻底移除 swarm 侧 `register_rail_type`。

### 9.3 内省与反射

- `list_elements() -> list[dict]`：全量 descriptor 的 JSON 列表（内省 / 未来配置 UI / loader）。
- `resolve_factory("module:qualname")`：importlib 反射还原工厂 / 类 / resolver。
- round-trip：`HarnessElementDescriptor.model_validate_json(d.model_dump_json())` 还原后 `factory_ref` 在任何可 import 进程还原为同一可调用。

### 9.4 registry.py

`register_swarm_providers()`（幂等闸门 `_REGISTERED`）= import 各 provider 模块（触发 `@harness_element` 填 catalog）→ `register_from_catalog()` → `register_build_context_factory(_build_swarm_context_from_seed)`。re-export 全部 `swarm.*` 名称常量供 `config_specs` 按符号引用。

---

## 10. 元素全量清单（36）

> P=param（属性，config_specs 烘焙）；C=context（环境，SwarmBuildContext）；—=无输入。
> 模式：T=team，K=code（code.team/team.plan）。

### 10.1 Tool（8，swarm 自有）

| name | 模式 | P（属性） | C（环境） |
|---|---|---|---|
| `swarm.skill_toolkit` | T+K | — | workspace_root |
| `swarm.user_todos` | T+K | — | — |
| `swarm.video` | T+K | — | config（models.video 门控） |
| `swarm.image_gen` | T+K | — | config（IMAGE_GEN_API_KEY 门控） |
| `swarm.xiaoyi_phone` | T+K | — | config（channels.xiaoyi 门控） |
| `swarm.code_extra_tools` | K | acp_enabled | — |
| `swarm.cron_tools` | T+K | — | member_card_id, channel_id, session_id, request_metadata, language |
| `swarm.send_file` | T+K | channels_config | request_id, channel_id, session_id, request_metadata |

> 通用 web / vision / audio 工具由 openjiuwen 提供（见 10.5）。

### 10.2 Rail — 工厂（18）

| name | 模式/角色 | P（属性） | C（环境） |
|---|---|---|---|
| `swarm.member_skill_toolkit` | T+K | —（不再收 skills：视图文档由 `core.team.skill_use` 单点播种） | workspace_root, visibility_path（`resolve_member_skill_visibility_path()` 派生，仅用于日志）, global_skills_dir |
| `swarm.runtime_prompt` | T | — | language, channel |
| `swarm.team_workspace_report_path` | T+K | — | team_ws_root, team_id, language |
| `swarm.context_processor` | T+K | context_engine_enabled, context_engine_config | — |
| `swarm.plugin_rails` | T+K | — | —（全局 rail manager） |
| `swarm.team_skill_evolution` | T+K / leader | evolution_model_config, auto_save（整条链路受 `skill_evolution` 开关门控） | global_skills_dir, language, role, team_id, trajectory_registry, channel, session_id, team_ws_root |
| `swarm.team_skill_create` | T+K / leader | —（params 恒为 `{}`，受 `skill_evolution` 开关门控） | global_skills_dir, team_skill_visibility_path, language, channel, session_id, team_ws_root, team_id, trajectory_registry |
| `swarm.member_skill_evolution` | T+K / teammate | evolution_model_config（受 `skill_evolution` 开关门控） | global_skills_dir, language, trajectory_registry, team_id, channel, session_id |
| `swarm.code_runtime_prompt` | K | — | language, channel |
| `swarm.code_project_memory` | K | additional_directories | project_dir, language |
| `swarm.permission_interrupt` | K | permissions_config, model_name | — |
| `swarm.code_coding_memory` | K | embed_config | project_dir, workspace_root（+ 写 ctx.extras） |
| `swarm.code_agent_mode` | K | — | — |
| `swarm.structured_ask_user` | K | — | language |
| `swarm.code_task_planning` | K | — | — |
| `swarm.code_agent_rail` | K | — | workspace_dir |
| `swarm.user_hooks` | K | hooks_section | — |

### 10.3 Rail — 类（3，无输入，`EmptyInput`）

`response_prompt`(T+K)、`stream_event`(T+K)、`avatar_prompt`(T)。

### 10.4 Sub-agent（1，code）

| name | P（属性） | C（环境） | 句柄（ctx.extras） |
|---|---|---|---|
| `swarm.code_agent`（门控） | max_iterations | workspace_root, language | `_parent_model`, `_coding_memory_rail` |

### 10.5 openjiuwen 提供（按 `core.*` 名引用，不在 swarm catalog）

归一后这些元素由 openjiuwen 声明 + 注册，swarm 经 `config_specs` 按 `core.*` 名引用（params 由 swarm 烘焙）：

- Rail：`core.sys_operation`、`core.task_planning`、`core.security`、`core.heartbeat`、`core.confirm_interrupt`(tool_names)、`core.worktree`(enabled)、`core.lsp`(project_dir)。
- Tool：`core.web_search`、`core.web_fetch`、`core.web_paid_search`、`core.vision`(vision_model_config)、`core.audio`(dedicated + audio_model_config)。vision/audio 的 config 由 swarm `tools.vision_model_config_params` / `audio_model_config_params` 从 yaml+env 填充后烘焙进 params。
- Sub-agent：`core.explore_agent`、`core.plan_agent`、`core.browser_agent`（language / max_iterations 为 params，model 取 `ctx.extras["_parent_model"]`）。

`registry.register_swarm_providers` 先调 `ensure_harness_elements_registered()` 确保上述 openjiuwen 元素已注册。

---

## 11. 端到端数据流（以 `core.team.skill_use` 为例）

skill 实体只存放在唯一的全局库，成员看得见哪些 skill 由 member / team 两份
`skills-visibility.json` 决定。成员名是 spawn 时才铸出来的，所以 claw 侧只声明
「怎么暴露」，身份类 params（库根、两份声明文件路径、seed allow）由 openjiuwen
的 `agent_teams.skill.rail_spec.complete_declared_team_skill_rails` 在成员装配时补齐。

```
config.yaml: react.skill_mode = "auto_list"
   │ enrich → config_specs._skill_mode(config) = "auto_list"
   ▼
RailSpec(type="core.team.skill_use",
         params={"skill_mode": "auto_list", "bootstrap_allow": [...]})   # 属性烘焙
   │ (随 DeepAgentSpec 序列化 / 跨进程重建保留)
   ▼
AgentConfigurator.setup_agent → complete_declared_team_skill_rails(...)
   │ 补 team_name / skills_dir / member_visibility_path / team_visibility_path
   ▼
RailSpec.build(language=..., context=SwarmBuildContext)
   │ openjiuwen: _RAIL_PROVIDER_REGISTRY["core.team.skill_use"](params, context)
   ▼
TeamSkillUseRail(skills_dir=<全局唯一库>, visibility_provider=...)
   │ 构造时用 bootstrap_allow 播种成员声明（唯一写者）
   │ 每轮按两份声明重算 allow/deny；空 allow = 继承全库，deny 优先
```

> 成员 `skills-visibility.json` 的**播种只有这一个写者**：
> `create_team_skill_use_rail`。`swarm.member_skill_toolkit` 不再写这份文档，
> 否则同一次装配里两个写者抢同一把文件锁，跳过规则还会各自漂移。

> 团队 `skills-visibility.json` 的**播种者不在 claw**：唯一写者是 openjiuwen 的
> `team_workspace/manager.py::initialize`（`_seed_team_skill_visibility()`），
> 团队 workspace 起来时必跑一次。`assembly.py` 只按 `team_ws_root` 算出
> `team_skill_visibility_path` 注入 `SwarmBuildContext` 供 rail 读取，**不写文件**；
> `TeamManager` 也不再播种。理由是"缺文件 = 不施加约束"——`read_skill_visibility`
> 对不存在的文件降级为空文档，`compose_skill_visibility` 里团队传空文档即无约束，
> 所以没有 workspace 的团队根本不需要这份文件。曾经三处各播一次，语义碰巧一致
> （空 allow + `AUTHORITY_SEED` + 不覆盖已存文件）才没出事；任一处改播非空 allow，
> 胜负就变成由调用顺序决定。回归护栏见
> `tests/unit_tests/agentserver/test_team_shared_skills.py::test_platform_declares_no_team_scope_skill_visibility_seeder`。

---

## 12. 序列化与跨进程重建

- `DeepAgentSpec` / `TeamAgentSpec` 全 pydantic，`model_dump_json()` / `model_validate_json()` 完整 round-trip；rails/tools 序列化为 `{type, params}`、subagents 为 `{factory_name, factory_kwargs, ...}`。
- 属性全在 `params`（含 `evolution_model_config` 等），跨边界自洽——重建侧无需再读 config 即可还原元素设定。
- 非序列化句柄（`config` / `trajectory_span_processor` / `extras`）经 `build_context_seed` + `register_build_context_factory` 在接收侧本地重建。

---

## 13. 如何新增一个 harness 元素

1. 在对应 `providers/*.py` 定义 `NAME = "swarm.xxx"` 常量并在 `registry.py` re-export。
2. 写 `XxxInput(ConstructionInput)`：属性用 `param_field`，环境用 `context_field(attr=... | resolver=...)`。
3. 写工厂 `build_xxx(params, ctx)`：首行 `inp = XxxInput.resolve(params, ctx)`，用 `inp.*` 构造；加 `@harness_element(kind=..., name=NAME, description=..., input_model=XxxInput)`。
4. 若有 config 派生属性：在 `config_specs.py` 加 `_extract_*` 并注册进 `_RAIL_PARAM_BUILDERS` / `_TOOL_PARAM_BUILDERS`（或在显式构造点注入 `params=`）；并把该元素名加入对应模式的 `_*_NAMES`。
5. subagent：经 `_code_subagent_spec` + `register_subagent_provider`。
6. 测试：`tests/agents/swarm/test_manifest_catalog.py`（catalog parity / source 标记 / schema 校验）+ `test_swarm_assembly.py`（构造回归）。

---

## 14. 测试与不变量

`tests/agents/swarm/`：
- `test_manifest_catalog.py`：常量 ↔ descriptor parity、catalog ↔ openjiuwen 注册表 parity、`factory_ref` / `resolver_ref` 反射、`input_schema` 校验、source 分类（属性=params / 环境=context）、`config_specs` 烘焙 params、JSON round-trip、注册幂等、接口自省、kind 覆盖。
- `test_swarm_assembly.py`：enrich / 折叠 / 跨进程重建 / 各元素构造的**行为回归闸门**。

合规：huawei-python-lint 0 违规 + ruff check/format。

---

## 15. 后续工作（未实现）

| 项 | 说明 |
|---|---|
| **配置文件 → harness loader** | 读元素清单（name + params），用 `input_model_ref` 校验 params，映射成 `RailSpec`/`BuiltinToolSpec`/`SubAgentSpec` → `DeepAgentSpec`。`input_schema` 的 source 标记已能区分「作者填的 params」与「运行时注入的 context」。需先定：context 派生值是否允许 params 覆盖。 |
| **前端 Agent Configuration 接入** | 把 `list_elements()` 经 CLI/HTTP 暴露，按 source 标记渲染表单。 |
| **base_tools 多模态拆解** | 当前 base_tools 多模态 / xiaoyi / paid_search 因 config+env+`os.environ` mutation 纠缠保持现状；后续把 enabled 开关 + 模型名抽成 attribute params、api_key 留 env、去 `multimodal_config` 的 env-mutation 副作用。 |
| **builder 内部收窄** | permission / coding_memory / context_processor / evolution 的本地 builder 当前收原 config 子 dict（swarm 隔离）；后续可收窄签名并迁移 legacy 调用方（interface_code / interface_deep / team_manager）。 |
| **interface_methods 精化** | 当前按 kind 自省基类契约；可按产出类精化（受惰性导入约束）。 |
| **上游 openjiuwen 对齐** | 把元数据 / 输入声明能力下沉到 openjiuwen 注册表。 |
