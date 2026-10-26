# Team 模式接入专家团 Manifest 加载设计

## 1. 背景与目标

单 Agent 已支持通过 AgentTemplate `manifest.json` 加载 persona、skills、tools、rails、MCP 和 subagents。Team 模式目前仍以通用的 `leader`、`teammate` 两类 `DeepAgentSpec` 构建成员，不能从一个专家团包生成固定成员及其独立配置。

本设计引入 `agent_group` 包类型，实现：

- 请求通过 `agent_group_name` 选择专家团。
- 专家团 manifest 决定 Leader 和固定成员名单。
- 每个成员加载自己的 AgentTemplate 和 persona。
- 专家团 Skill 对所有成员可用。
- AgentTemplate 快照进入 `DeepAgentSpec`，支持 TeamSpec 序列化。
- 每次新建 NativeHarness 时从 AgentSpec 快照重新挂载 AgentTemplate。
- manifest 或资源加载失败时终止 Team 构建。

设计基线：

- jiuwenclaw：`578fb2826fbb86bc26c11ef50508c569e4c369e1`
- agent-core：`eefde52e0f57508c2441a3cf5c5390648e5bb672`

本期不实现专家团商城、活动 Team 热切换、磁盘文件自动监听和跨物理节点的包自动分发。

## 2. 包目录

```text
sample-expert-group/
├── manifest.json
├── agents/
│   ├── leader/
│   │   ├── manifest.json
│   │   ├── AGENT.md
│   │   └── persona/
│   │       └── leader.md
│   ├── member1/
│   │   ├── manifest.json
│   │   └── persona/
│   │       └── member1.md
│   └── member2/
│       ├── manifest.json
│       └── persona/
│           └── member2.md
├── skills/
│   └── skill_name_1/
│       └── SKILL.md
└── README.md
```

目录规则：

- `agents/leader` 是固定的 Team Leader。
- Leader 的 `AGENT.md` 描述任务拆解、成员调度、质量控制和最终汇总规则。
- 普通 Member 不包含 `AGENT.md`，只包含 `manifest.json` 和 `persona/`。
- Member 的专业能力、团队职责、交付格式和工作边界写在自己的 persona 中。
- 顶层 `skills/` 存放所有成员共享的 Skill。

标准包架位置：

```text
jiuwenswarm/resources/agent/workspace/plugins/agent_groups/

<agent-workspace>/plugins/agent_groups/
├── built_in/
└── local/
```

## 3. Manifest 协议

### 3.1 顶层 AgentGroup Manifest

顶层 manifest 定义运行时需要的字段：

```json
{
  "name": "sample-expert-group",
  "package_type": "agent_group",
  "instruction": "Leader 负责任务拆解和最终汇总，member1 负责方案分析，member2 负责风险复核。",
  "agents": [
    "leader",
    "member1",
    "member2"
  ],
  "skills": [
    "skill_name_1"
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---:|---|
| `name` | 是 | 专家团 ID，必须与包目录名一致 |
| `package_type` | 是 | 固定为 `agent_group` |
| `agents` | 是 | 固定成员列表，必须包含 `leader` |
| `instruction` | 否 | 所有成员共享的协作说明，默认空字符串 |
| `skills` | 否 | 所有成员共享的 Skill 目录名，默认空列表 |

展示名称、描述、分类和扩展字段不属于本期运行协议。

### 3.2 Leader Manifest

`agents/leader/manifest.json`：

```json
{
  "packageType": "agent_template",
  "agentCard": {
    "id": "leader",
    "name": "专家团负责人"
  },
  "persona": {
    "dir": "."
  }
}
```

`persona.dir` 指向当前目录，使现有 AgentTemplate loader 同时读取：

- `AGENT.md`
- `persona/leader.md`

### 3.3 Member Manifest

`agents/member1/manifest.json`：

```json
{
  "packageType": "agent_template",
  "agentCard": {
    "id": "member1",
    "name": "方案分析专家"
  },
  "persona": {
    "dir": "./persona"
  }
}
```

Member persona 应包含角色定位、专业能力、团队职责、分析要求、交付格式和工作边界。`member2` 使用相同结构并指向自己的 persona。

## 4. 接口与数据模型

本设计只增加三个公共接口点。

### 4.1 请求参数

`ChatSendParams` 新增：

```python
agent_group_name: NotRequired[str]
```

语义：

- 首次 Team 构建携带非空名称：选择对应专家团。
- 字段缺失：使用会话已绑定的专家团；没有绑定时保持普通 Team。
- 空字符串、非法类型或活动 Team 请求切换名称：返回错误。

首次构建成功后，将 `agent_group_name` 保存到 session metadata，供整个 TeamSpec 冷恢复时使用。

### 4.2 DeepAgentSpec

`DeepAgentSpec` 新增：

```python
agent_template_spec: dict[str, Any] | None = None
```

字段保存当前成员已解析的 AgentTemplateSpec：

```python
agent_template_spec.model_dump(mode="json")
```

该字段使用 `dict`，以避免 `deep_agent_spec` 与 `extension_spec` 循环导入，并保持 TeamSpec JSON round-trip 能力。

### 4.3 DeepAgent

`DeepAgent` 新增与现有 `load_plugin_spec()` 对称的接口：

```python
async def load_agent_template_spec(
    self,
    spec: AgentTemplateSpec,
    *,
    context: BuildContext | None = None,
) -> LoadRecord:
    ...
```

该方法复用现有 `resolve_agent_template_parts`、`apply_extension_hot` 和失败回滚逻辑，不新增另一套资源挂载机制。

## 5. 包解析

jiuwenclaw 在现有 `extension_package_manager` 中增加一个内部函数：

```python
resolve_agent_group_dir(name: str) -> Path
```

它在以下位置查找包：

1. `<agent-workspace>/plugins/agent_groups/local/<name>`
2. `<agent-workspace>/plugins/agent_groups/built_in/<name>`
3. packaged resources 中的 `plugins/agent_groups/<name>`

没有匹配包时返回不存在错误；多个位置存在同名包时返回冲突错误。请求只允许传单段包名，不接受路径。

AgentGroup 的解析由一个内部函数完成：

```python
load_agent_group_package(path: Path) -> dict[str, AgentTemplateSpec]
```

处理流程：

1. 读取并校验顶层 manifest。
2. 对每个 `agents/<name>/manifest.json` 调用现有 AgentTemplate loader。
3. 将顶层 `instruction` 添加为公共 prompt section。
4. 将顶层共享 Skill 转换成绝对路径 `SkillSpec`。
5. 返回以 agent name 为键的 AgentTemplateSpec。

不新增独立的 Resolver 或 Loader 类。

## 6. TeamSpec 组装

组装顺序：

1. 加载现有 Team 配置。
2. 使用 `enrich_team_spec_for_swarm()` 生成基础 `leader` 和 `teammate` specs。
3. 加载选定的 AgentGroup 包。
4. 将每个 AgentTemplateSpec 序列化后写入对应的 `DeepAgentSpec.agent_template_spec`。
5. 生成固定成员并设置 Team 模式。

先执行现有 provider enrichment，保证专家成员继续拥有 jiuwenclaw 提供的 Team rails、tools、权限、workspace 和 MCP。

### 6.1 Leader

`leader` 映射到：

```python
team_spec.agents["leader"]
```

现有 Team Leader 的运行时 member name 保持不变，只写入 AgentTemplateSpec 和共享 Skill 声明。

### 6.2 Member

对 `agents` 中除 `leader` 外的每个成员：

1. 复制已 enrichment 的通用 `teammate` spec。
2. 写入该成员的 `agent_template_spec`。
3. 以成员名写入 `team_spec.agents`。
4. 创建同名 predefined member。

```python
team_spec.agents["member1"] = member1_spec
team_spec.agents["member2"] = member2_spec
```

```python
TeamMemberSpec(
    member_name="member1",
    display_name="方案分析专家",
    role_type="teammate",
)
```

AgentConfigurator 已支持优先按 `member_name` 查找 spec，因此每个 Member 会取得自己的配置。

### 6.3 固定成员模式

专家团启用时：

```python
team_spec.team_mode = "predefined"
```

专家团成员替换基础 Team 配置中的 `predefined_members`。Leader 不再暴露动态 spawn teammate 工具，Team 只启动 manifest 声明的固定成员。

## 7. NativeHarness 加载流程

NativeHarness 保持同步构造，在异步准备阶段加载 AgentTemplateSpec：

```text
NativeHarness.__init__
  └── 根据 DeepAgentSpec 构建基础配置、tools 和 rails

NativeHarness._prepare
  ├── AgentTemplateSpec.model_validate(agent_template_spec)
  ├── await load_agent_template_spec(...)
  ├── await ensure_initialized()
  └── 标记 prepared
```

加载发生在 `ensure_initialized()` 之前，确保 manifest 中的 rail、MCP 和 Skill 在运行前完成初始化。`start()` 和 `run_once()` 都通过 `_prepare()`。

重载语义：

- 同一个 NativeHarness 实例只加载一次。
- TeamHarness 重建 NativeHarness 时，从其持有的 AgentSpec 快照重新加载。
- 只有重新组装 TeamSpec 才重新读取磁盘 manifest。
- 已经生成的 AgentSpec 不受后续磁盘文件修改影响。

## 8. Skill 集成

顶层 `skills` 对所有成员生效。

AgentGroup loader 校验 `skills/<name>/SKILL.md` 并生成绝对路径 `SkillSpec`。组装成员 spec 时，将 Skill 目录名与基础 `DeepAgentSpec.skills` 去重合并，使其进入 TeamSkillUseRail 的初始可见性声明。

NativeHarness 加载 AgentTemplateSpec 时，将 Skill 路径挂到已有 TeamSkillUseRail，不创建第二套 Skill rail。

## 9. 校验与错误处理

加载前必须验证：

- `package_type == "agent_group"`。
- `name` 与包目录名一致。
- `agents` 是非空、无重复的字符串列表并包含 `leader`。
- agent 名是安全的单段目录名。
- 每个 Agent 目录和成员 manifest 存在。
- 成员 manifest 的 `packageType == "agent_template"`。
- `agentCard.id` 与 Agent 目录名一致。
- Leader 存在 `AGENT.md` 和 persona Markdown。
- Member 的 persona 目录至少包含一个 Markdown。
- 每个共享 Skill 存在 `SKILL.md`。
- 所有资源路径位于对应包根目录内。

任一校验、反序列化或资源挂载失败时：

1. 回滚本次已经挂载的资源。
2. 终止 Team 构建或成员启动。
3. 通过现有 `chat.error` 返回错误。
4. 不允许以部分成员成功的状态继续运行。

## 10. 兼容与分布式约束

- `agent_group_name` 缺失时保持普通 Team 行为。
- `agent_template_spec` 默认值为 `None`，不影响现有 DeepAgentSpec。
- 现有文件版 AgentTemplate、Plugin API 和 provider enrichment 保持不变。
- TeamSpec 序列化后可通过 `DeepAgentSpec.model_validate()` 恢复快照。
- persona 和 instruction 内容进入快照；Skill、tool、rail 等文件型资源仍使用路径引用。
- 同机多进程可共享包路径；跨物理节点必须预先安装同一专家团包并保证资源路径可访问。
- 远端资源缺失时构建失败，不回退到普通 teammate。

## 11. 测试与验收

### 11.1 agent-core

- `DeepAgentSpec` 可以序列化和恢复 `agent_template_spec`。
- `load_agent_template_spec()` 能挂载 persona、公共提示和 Skill。
- 资源挂载失败时完整回滚。
- NativeHarness 在 `ensure_initialized()` 前完成加载。
- 同一 NativeHarness 不重复加载，重建 NativeHarness 后重新加载快照。
- `start()` 和 `run_once()` 行为一致。

### 11.2 jiuwenclaw

- 可以通过 `agent_group_name` 解析 `sample-expert-group`。
- Team 自动创建 Leader、member1 和 member2。
- member1、member2 进入 `predefined_members`，Team 使用 `predefined` 模式。
- Leader 加载 `AGENT.md` 和自己的 persona。
- Member 只加载自己的 persona，不依赖 `AGENT.md`。
- 三个成员均可使用 `skill_name_1`。
- AgentConfigurator 按 member name 取得精确 spec。
- session metadata 能在 TeamSpec 冷恢复时恢复专家团选择。
- 非法 manifest、缺失资源、名称不一致和路径穿越均阻止 Team 启动。
- 未携带 `agent_group_name` 的原有 Team 测试保持通过。

满足以上测试，并且 TeamHarness 重建后 persona、instruction 和 Skill 仍然生效，即完成本期功能。
