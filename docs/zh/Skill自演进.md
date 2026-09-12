# Skill 自演进

## 1. 功能概览

### 1.1 Skill 自演进简介

Skill 自演进是 JiuwenSwarm 基于 openJiuwen 自演进框架实现的一项核心功能，它打破了传统 Agent 系统能力固定的局限。传统 Agent 系统的能力定义一旦写好，就基本不会再变——工具调用出错仅记录日志，用户反馈理解有误但下次仍使用同样逻辑。能力的上限从部署那天就已固定。

JiuwenSwarm 的 Skill 自演进机制将真实使用中反复出现的问题和更好做法转化为 Skill 的改进输入。它让 Skill 不再是一次性的静态文档，而是能够随着真实使用持续迭代的活文档。经验保存后，Agent 再次使用该 Skill 时会自动加载，无需立即改写 `SKILL.md`。

当前主流程不会因某个错误关键词或一次用户纠正就必然生成经验。对于 Single Agent 和 Agent Team 的 Team Leader，主 Agent 会根据当前任务证据判断改进是否可复用，再决定是否建议发起演进。

### 1.2 核心价值

Skill 自演进机制的核心价值在于：

- **降低日常干预成本**：智能体识别可复用经验，再按审批配置保存。
- **持续能力提升**：随着使用时间增加，Skill 可以通过累积的纠错、预检、降级策略和验证方法提高准确性和可靠性。
- **自适应场景变化**：根据真实使用场景沉淀可复用的调整和优化。
- **降低维护成本**：减少手动更新和维护 Skill 的工作量，并可通过查看、整理、重建和回滚管理已累积的经验。

## 2. 配置与角色差异

### 2.1 启用 Skill 自演进

Skill 自演进由 `react.evolution.skill_evolution` 统一开关控制，默认为 `false`。Web 配置页将该开关显示为 **本地技能自动演进与沉淀**，TUI 在 Features 分组中显示为 **技能演进与创建**。

最小配置如下：

```yaml
react:
  evolution:
    skill_evolution: true
    auto_save: false
```

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `react.evolution.skill_evolution` | `false` | 统一启用 Skill 演进、自动 Skill 创建建议及相关命令和工具 |
| `react.evolution.auto_save` | `false` | YAML-only 高级选项；控制 Single Agent 和 Team Leader 的经验提交是否需要用户审批 |
| `react.evolution.review_feedback_min_confidence` | `0.7` | Reviewer Feedback 归因进入团队演进流程的最低置信度 |

关闭 `skill_evolution` 会禁用相关 Rails、自检提示、演进工具和 `/evolve` 命令，但不影响用户显式使用通用 `skill-creator` 或 `swarmskill-creator`。

升级时，系统会按新模板同步配置结构，但不会将旧版 `enabled`、`auto_scan`、`skill_create` 或相关环境变量的取值转换为 `skill_evolution`。如果升级前已启用过相关能力，请在升级后重新确认并显式开启 **Skill 自演进**。

![打开本地技能自动演进与沉淀](../assets/images/skill演进_开关.png)

### 2.2 角色差异

| 角色 | 触发和审批方式 |
| --- | --- |
| Single Agent | 主 Agent 按非 follow-up 任务轮次计数，当前默认每 5 轮自检一次；是否审批由 `auto_save` 决定 |
| Team Leader | 每次团队任务确认完成后自检当次团队执行；审批同样由 `auto_save` 控制，并面向用户展示建议和审批交互 |
| Teammate | 使用后台被动信号链路并固定自动保存，不展示与上述主流程相同的自检建议和审批交互 |

## 3. 演进触发与管理

### 3.1 Agent 自动建议

开启功能后，Single Agent 和 Team Leader 在不同时机发起自检：

- **Single Agent**：按非 follow-up 任务轮次计数，当前默认累计 5 轮后检查一次。后台 heartbeat、cron 和 follow-up 任务不计入该阈值。
- **Team Leader**：不使用上述 5 轮阈值；每次团队任务被确认为完成后，对该次团队执行发起一次自检。

触发自检后，主 Agent 会判断当前任务是否包含值得保留的可复用更新，例如：

- 可以应用到后续同类任务的用户纠正。
- Skill 缺少必要的预检、参数说明、降级策略或验证步骤。
- 某项执行失败暴露出可重复修复的 Skill 指令缺口。

临时环境故障、一次性事实、个人偏好或证据不足的推测不应生成演进建议。如果主 Agent 判断存在可复用更新，它会在正常回复末尾简要说明改进点，并询问是否发起 Skill 演进；否则不会向用户暴露这次内部自检。

![Agent 判断存在可复用改进并建议发起演进](../assets/images/skill演进_Agent自动建议.png)

> **前置条件**：开启 **Skill 自演进**，并确保目标 Skill 已安装且可见。使用 Single Agent 时，完成 5 个符合条件的非 follow-up 任务轮次；使用 Team Leader 时，完成一次团队任务并使任务状态被确认为全部完成。

### 3.2 Reviewer Feedback 驱动的团队演进

在 scheduled 团队中，如果 Task 验收失败且已开启 Skill 自演进，系统会对 Reviewer Feedback 进行归因：

- 归因为 Skill 缺陷时，系统保留 observation，并在全部 Task 完成后按 Skill 汇总，交给 Team Skill 演进流程。
- 归因为执行者失误或无法归因时，只记录失败，不修改 Skill。
- 没有可归因 Skill、但相同可复用模式跨 Task 重复出现时，系统会发起新 Team Skill 审批。

只有达到 `react.evolution.review_feedback_min_confidence` 阈值的归因结果才会进入上述流程。Reviewer Feedback 不会创建或更新成员私有 Skill 副本，也不使用 Single Agent 的 5 轮计数逻辑。

已有 Team Skill 的更新沿用 Team Leader 的演进审批流程，默认 `auto_save: false` 时需要用户审批；新 Team Skill 创建使用独立的创建审批流程。

### 3.3 使用 `/evolve` 主动发起

如果希望立即审查某个 Skill，可以输入：

```text
/evolve <skill_name> [user_intent]
```

`user_intent` 是可选的演进意图，可以用来指明希望改进的问题。例如：

```text
/evolve xlsx 增加处理合并单元格前的预检和失败恢复说明
```

系统会审查当前任务中可用的对话和执行证据，然后返回“无需演进”或结构化改进提案。`/evolve` 是发起审查，不代表一定会生成或保存经验。

自动建议仍遵循运行模式的对象边界：Single Agent 面向普通 Skill，Team Leader 面向 Team/Swarm Skill。显式使用 `/evolve` 时，系统会按目标的实际类型发起审查，因此 Team 模式可以手动演进普通 Skill，普通模式也可以手动演进 Team/Swarm Skill；目标仍需已安装且对当前 Agent 可见。

![使用 evolve 命令主动发起并生成待审批提案](../assets/images/skill演进_命令触发.png)

> **前置条件**：开启 **Skill 自演进**，确保目标 Skill 已安装且对当前 Single Agent 或 Team Leader 可见，然后输入带 Skill 名称的 `/evolve` 命令。如需展示审批交互，还需设置 `auto_save: false`。

### 3.4 审批和保存

对于 Single Agent 和 Team Leader，`react.evolution.auto_save` 使用相同的审批规则：

- `auto_save: false`：提案通过校验后进入用户审批，确认后才写入经验库。
- `auto_save: true`：提案通过校验后跳过用户审批并自动保存。

Teammate 使用固定自动保存策略，不按上述 `auto_save` 值展示审批交互。经验保存后，后续调用该 Skill 时会自动加载。

`auto_save: false` 时，Single Agent 或 Team Leader 会向用户呈现经验审批入口。

![Skill 演进经验审批入口](../assets/images/skill演进_审批.png)

展开审批内容后，可以查看结构化提案的 `target`、`section`、`reason` 和 `content`，再决定是否批准。

![查看并审批结构化 Skill 演进提案](../assets/images/skill演进_审批详情.png)

### 3.5 查看和整理经验

优先使用 Web 管理已保存的经验。在技能列表中找到目标 Skill，然后点击 **查看技能经验**。

![从技能列表打开技能经验](../assets/images/skill演进_技能经验入口.png)

打开经验编辑器后，可以查看每条记录、修改经验内容、删除记录并保存。

![在 Web 中查看和编辑已保存的 Skill 经验](../assets/images/skill演进_技能经验.png)

也可以使用命令：

```text
/evolve_list <skill_name>
/evolve_simplify <skill_name> [user_intent]
```

`/evolve_list` 显示指定 Skill 的经验摘要；`/evolve_simplify` 审查已有经验，并对合并、精炼或删除建议执行相应流程。

![使用命令查看和整理 Skill 经验](../assets/images/skill演进_查看与整理经验.png)

### 3.6 重建和回滚

已保存的经验不需要重建就能在后续调用中生效。如果希望将经验永久合并到 `SKILL.md`，使用：

```text
/evolve_rebuild <skill_name> [user_intent]
```

重建会先归档当前 Skill 和经验日志，再将经验合入 `SKILL.md`；已合入的经验不再作为独立演进条目保留。

![将已保存经验重建进 Skill](../assets/images/skill演进_重建.png)

查看可用归档或恢复指定版本时使用：

```text
/evolve_rollback <skill_name>
/evolve_rollback <skill_name> <version>
/evolve_rollback <skill_name> latest
```

不提供版本时会列出可用归档；提供具体版本或 `latest` 时会执行恢复。恢复前的当前状态也会自动归档，以便再次回滚。

![查看并恢复 Skill 重建归档](../assets/images/skill演进_回滚.png)

### 3.7 `evolutions.json` 高级排障

系统将经验保存在 Skill 目录下的 `evolutions.json`。该文件在首次保存经验时动态创建，没有任何已保存经验时可能不存在。

```text
~/.jiuwenswarm/agent/workspace/skills/<skill_name>/
├── SKILL.md
├── evolutions.json    # 首次保存经验后创建
└── ...
```

Agent Team 使用同一个全局 Skill 库，因此经验仍保存在上述路径。成员可见的 Skill 由团队的可见性声明决定，详见 [Agent Team](AgentTeam.md) 的“Team Skills”小节。

下面是当前存储结构的**只读示例**：

```json
{
  "skill_id": "file-operations",
  "version": "1.0.0",
  "updated_at": "2026-08-17T10:30:00+00:00",
  "entries": [
    {
      "id": "ev_1234abcd",
      "source": "user_intent",
      "timestamp": "2026-08-17T10:30:00+00:00",
      "context": "Relative file path failed before checking the working directory",
      "change": {
        "section": "Troubleshooting",
        "action": "append",
        "content": "读取相对路径前，先确认工作目录和候选路径。",
        "target": "body"
      },
      "applied": false,
      "score": 0.6,
      "usage_stats": {
        "times_presented": 0,
        "times_used": 0,
        "times_positive": 0,
        "times_negative": 0
      },
      "summary": "增加相对路径预检"
    }
  ]
}
```

- `entries` 包含已保存的经验记录。
- `change` 描述一条经验的改进位置、操作和具体内容。
- `target` 表示改进所属的 Skill 层，可以是 `description`、`body` 或 `script`。

该示例用于帮助理解和排障，不是手工创建记录的接口契约；系统可能随版本扩展可选字段。Web 经验编辑器和演进命令是首选管理入口。只在排障或恢复时直接编辑 `evolutions.json`，并在编辑前备份文件、编辑后校验 JSON 语法。不要手动新增记录或改写 `id`、`source`、`timestamp` 等系统管理字段。

## 4. 案例：为文件读取 Skill 补充预检

### 4.1 场景背景

假设我们有一个简单的 `file-operations` Skill，用于处理文件操作。该 Skill 包含一个 `read_file` 功能，用于读取文件内容。

**初始 `SKILL.md` 定义：**

```markdown
# file-operations Skill

## Functions

### read_file
- 功能：读取指定文件的内容
- 参数：
  - `file_path`: 文件路径
- 返回：文件内容

## Examples
- 读取文件：read_file({"file_path": "/path/to/file.txt"})
```

这份初始说明没有要求 Agent 在读取相对路径前确认工作目录和候选路径。

### 4.2 在多轮对话中累积证据

#### 第一轮：发现问题

```text
User: 帮我读取当前目录下的 test.txt 文件。
Assistant: 好的，我将为您读取当前目录下的 test.txt 文件。
Tool: file-operations.read_file({
  "file_path": "test.txt"
})
Tool Response: Error: File not found: test.txt
Assistant: 对不起，我找不到 test.txt 文件。请问文件的完整路径是什么？
```

这次执行失败可以作为演进审查的证据，但不会因 `Error` 字样就必然生成经验。

#### 第二轮：用户提供可行路径

```text
User: 应该是 ./test.txt。
Assistant: 好的，我将为您读取 ./test.txt 文件。
Tool: file-operations.read_file({
  "file_path": "./test.txt"
})
Tool Response: 这是 test.txt 文件的内容。
Assistant: 已成功读取 ./test.txt 文件。
```

#### 第三轮：用户补充可复用要求

```text
User: 不对，当我只提供文件名时，你应该先确认当前目录，不需要我手动添加 ./。
Assistant: 非常感谢您的反馈。我会先确认当前目录和候选路径，再读取文件。
```

这条纠正进一步说明了失败的原因和希望复用的做法。在 Single Agent 的计数自检或 Team Leader 的团队任务完成自检中，主 Agent 会判断“在读取相对路径前确认工作目录和候选路径”是否适用于后续同类任务，而不是根据“不对”等关键词直接生成记录。

### 4.3 审查并保存提案

如果主 Agent 判断该做法可复用，它会询问用户是否为 `file-operations` 发起演进。用户也可以不等待自检，直接输入：

```text
/evolve file-operations 增加读取相对路径前的工作目录和候选路径预检
```

发起后，`evolution_reviewer` 会审查当前证据，并在确认值得演进时生成结构化提案。提案经审批或自动保存后，会写入 `file-operations` 的经验库。

### 4.4 演进后的效果

下次调用 `file-operations` 时，Agent 会自动加载这条经验并按其中的预检方法执行：

```text
User: 帮我读取当前目录下的 test.txt 文件。
Assistant: 我会先确认当前目录，然后读取 ./test.txt。
Tool: file-operations.read_file({
  "file_path": "./test.txt"
})
Tool Response: 这是 test.txt 文件的内容。
Assistant: 已成功读取 test.txt 文件。
```

现在，当用户只提供文件名时，Agent 可以按已保存的经验先确认当前目录和候选路径，不再要求用户手动补充 `./` 前缀。

## 5. 工作原理

### 5.1 关键组件

- **`SkillEvolutionRail` / `TeamSkillEvolutionRail`**：注册演进工具和审查 Subagent，并组织自检、提交和演进生命周期。
- **`evolution_reviewer` Subagent**：只使用受限的只读演进工具审查当前证据，判断是否需要演进并生成结构化提案。
- **`EvolutionInterruptRail`**：在需要人工确认时承接审批交互。
- **`EvolutionStore`**：负责查询、保存和重建经验数据。

### 5.2 Single Agent 和 Team Leader 主流程

```text
Single Agent 计数自检，或 Team Leader 任务完成自检，或 /evolve
        │
        ▼
主 Agent 判断是否存在可复用更新
        │
        ▼
用户确认发起（自动自检场景）
        │
        ▼
evolution_reviewer 审查证据并生成提案
        │
        ▼
提案校验
        │
        ├─ auto_save: false → EvolutionInterruptRail 审批
        └─ auto_save: true  → 自动保存
                              │
                              ▼
                       EvolutionStore
                              │
                              ▼
                       evolutions.json
```

### 5.3 Teammate 兼容链路

Teammate 不使用上述面向用户的自动自检交互，而是保留简化的被动链路：

```text
被动信号检测 → SkillExperienceOptimizer 生成候选经验 → 固定自动保存
```

`SkillExperienceOptimizer` 仍服务于这条被动链路，但不负责 Single Agent、Team Leader 或 `/evolve` 的主要判断和提案流程。

## 6. 命令速查

| 命令 | 作用 |
| --- | --- |
| `/evolve` | 查看所有可见 Skill 的 pending 经验摘要 |
| `/evolve <skill_name> [user_intent]` | 为指定 Skill 发起审查 |
| `/evolve_list <skill_name>` | 查看指定 Skill 的经验摘要 |
| `/evolve_simplify <skill_name> [user_intent]` | 整理指定 Skill 的经验 |
| `/evolve_rebuild <skill_name> [user_intent]` | 将经验重建进 Skill |
| `/evolve_rollback <skill_name> [version]` | 查看可恢复版本，或将 Skill 恢复到指定版本 |

---

## 返回导航

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)
