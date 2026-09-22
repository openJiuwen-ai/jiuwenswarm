# Team Organization 使用指南

Team Organization 在 Agent Team 之上组织多个独立 Team。每个 Team 继续管理自己的成员；各 Team Leader 通过共享任务池、可靠消息和组织工作空间共同完成一个根任务。

> Team Organization 目前通过 Team Leader 自主调用 `org_*` 工具运行，没有单独的 `/organization` 命令。请先进入 Team 模式，再用明确的提示词要求 Leader 创建组织、专家 Team 和任务树。

---

## 一、适用场景

适合使用 Team Organization 的任务通常具备以下特点：

- 需要研发、测试、调研、审核等多个专业团队协作。
- 各团队需要独立上下文和成员，但共享任务状态与交付物。
- 子任务结果需要上游 Team 明确验收。
- 希望任务、消息和汇总过程在中断后恢复。

单个 Team 内可以完成的角色分工，仍建议使用普通 Agent Team。Team Organization 当前同一时间只处理一个非终态根任务。

## 二、运行前配置

先按[快速开始](Quickstart.md)完成初始化和模型配置。Team Organization 复用 `modes.team.jiuwen_team`，不需要新增顶层 `organization` 配置。以下是与本功能直接相关的最小配置片段：

```yaml
modes:
  team:
    jiuwen_team:
      team_name: jiuwen_team
      lifecycle: persistent
      teammate_mode: build_mode
      spawn_mode: inprocess

      leader:
        member_name: team-leader
        display_name: 团队领导
        persona: "项目负责人，擅长跨团队拆解、验收和风险协调"

      agents:
        leader: $agent_leader

      workspace:
        enabled: true

      transport:
        type: inprocess

      storage:
        type: sqlite
```

其中 `$agent_leader` 必须能解析到已配置模型。Summary Team 会读取同一份 Team 默认模型配置；如果默认模型缺失，Summary Team 无法启动。单机联调可使用 SQLite 和 `inprocess`。分布式部署应让所有 Team 访问同一 PostgreSQL 数据库，并另行提供共享产物存储，详见[分布式 Team](分布式Team.md)。

## 三、端到端测试用例

下面的用例参考真实公开资料，验证当前投资与财务专家团能否组织法律、技术和市场专家团协作，完成验收并生成最终投资建议。

### 3.1 准备专家团

至少安装投资与财务、法律合规、技术尽调与 Coding、市场与商业四个合法 AgentGroup 包。当前与用户对话的 Team 使用投资与财务专家团，其余三个由 Leader 按需创建。包目录必须包含 `manifest.json`，例如：

```json
{
  "package_type": "agent_group",
  "name": "investment-finance"
}
```

`name` 必须与目录名一致。包还应提供可加载的 AgentGroup 定义、Leader 模板和能力标签。JiuwenSwarm 会从 local、built-in 和 resources 三类 AgentGroup 根目录扫描；同名包同时出现在多个来源时会因冲突而跳过。具体制作与安装规则见[专家团与汇总配置](TeamOrganization专家团与汇总配置.md)。

### 3.2 启动 JiuwenSwarm

```bash
uv sync
uv run jiuwenswarm-init
uv run jiuwenswarm-start dev
```

打开 Web 或 TUI，创建新会话并切换到 Team 模式：

```text
/mode team
```

### 3.3 发送测试提示词

将下面的提示词作为一条完整消息发送。它只描述目标、资料边界、专家分工和验收要求，不要求用户了解内部工具或状态名。

```text
请组织现有的投资与财务、法律合规、技术尽调与 Coding、市场与商业四个专家团，对 Zed Industries 做一次快速公开资料投资初筛。最终请直接向我交付建议正文。

这是一项协作流程联调，不是完整尽调。只回答一个问题：是否值得进入下一轮正式尽调？仅使用以下三个公开来源；不要扩展检索、调查竞品融资或估值、克隆代码仓、构建代码或运行测试：

- 公司与产品：https://zed.dev/about
- 开源和许可证说明：https://zed.dev/software-overview
- 代码仓：https://github.com/zed-industries/zed

请建立协作组织，邀请法律合规、技术尽调与 Coding、市场与商业三个专家团加入，与当前投资与财务专家团共同完成工作。认领整体事项的负责人应根据各团实际职责选择合适的汇总方式，优先考虑由独立汇总团队整合已验收的成果。当前专家团的投资与财务意见也必须作为一项可验收的正式成果。

请把以下四项工作放入共享任务池，等待具备相应能力的专家团认领，不要直接指定给某个内部成员。每项只交付一份短意见：正文不超过 120 个汉字（URL 和代码路径不计入），包含一项已确认发现、一项风险或待核验事项，并标注最多一个上述来源链接。不再拆分内部研究工作，也不要撰写长报告。

1. 投资与财务：依据公司介绍，指出一项投资前提，以及下一轮必须索取的一项财务材料。
2. 法律合规：只看官方许可证说明，指出一项许可证事实和一项待核验风险。
3. 技术与 Coding：实际查看公开代码仓的 README、许可证文件，以及 Cargo.toml 或一处核心代码/配置；指出一项有文件路径依据的工程事实和一项待核验风险。
4. 市场与商业：只依据产品介绍，指出一项产品机会和一项商业不确定性。

每项完成后，由认领整体事项的负责人先验收；四项都通过后再汇总。其他专家团执行期间不要反复轮询或催促，等待完成通知后继续；不要提前结束整体事项。

最终直接展示一份 400～600 个汉字的中文投资建议，不要只报告状态、文件路径或“正在等待”。建议必须包括：

- 明确结论：建议推进、附条件推进或不建议推进；
- 四个领域各自最关键的一条依据；
- 进入下一轮正式尽调前必须核验的事项，以及结论的不确定性。

不允许虚构营收、估值、客户数量、融资条款或安全结论。无法由上述公开资料确认的内容，一律写为“待核验”。
```

### 3.4 预期结果

测试通过时，应观察到以下里程碑：

1. 当前投资与财务 Team 创建协作组织，另外三个专家 Team 成功加入。
2. 整体事项由投资与财务 Team 认领；四项领域工作进入共享任务池并由能力匹配的 Team 认领。
3. 投资与财务意见也形成独立子任务成果，而不是只存在于当前 Team 的内部记录。
4. 四项短意见均完成并由整体事项负责人验收通过。
5. Organization 首次需要汇总时懒加载一个 Summary Team，而不是为每个来源创建 Summary Team。
6. Summary Task 从 `WAITING_SOURCES` 进入执行并完成，根任务最终为 `COMPLETED`。
7. 最终回复直接展示 400～600 字建议正文，并把无法确认的信息标为“待核验”；Team 面板或 Web 进度区域可看到 Organization 里程碑。

可以在同一会话补充以下检查提示词：

```text
请检查刚才的协作过程，不要创建新工作：告诉我有哪些专家团参与、四项工作是否都已完成并通过验收、最终汇总是否完成，以及最终建议保存在哪里。如有尚未处理的团队消息，也请说明。
```

## 四、常见问题

| 现象 | 检查项 |
|------|--------|
| `org_list_expert_groups` 返回空 | AgentGroup manifest、包名、能力标签以及同名冲突 |
| 专家 Team 创建后未入组 | Team 是否成功激活、是否共享 Owner 的 `TeamDatabase` |
| Summary Team 启动失败 | 默认模型是否可解析，Owner Team 是否已有数据库和 workspace |
| 子任务完成但父任务无法结束 | 子任务是否已有 `ACCEPTED` 审核，修复任务是否正确关联 |
| 最终文件找不到 | workspace 是否启用；分布式节点是否真正共享文件存储 |
| 重启后没有继续执行 | 是否使用同一 session 和持久化 storage，Team 是否已重新激活 |
