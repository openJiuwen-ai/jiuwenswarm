# 设计文档迁移与跨仓引用

2026-10-26：JiuwenSwarm 应用及应用接入设计从 agent-core 迁至 JiuwenSwarm 仓库；core 保留通用技能和底层设计。
设计正文、历史编号、审批与日期保持原样，附件随文档迁移。路径变化不表示方案已实现或重新通过评审。
本次没有迁移 core 的 specs、features、bugfix，跨仓设计仍引用各自的真实实现与契约。

| 设计 | 当前维护位置 | 归属依据 |
|---|---|---|
| SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation | [文档](archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | JiuwenSwarm TUI 接入与交互 |
| SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability | [文档](archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | TUI 嵌套工作流与预算展示 |
| SDD-0028-swarmflow-verify-fork-observability | [文档](archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | core 事件与 JiuwenSwarm 前端联合设计，随应用呈现侧维护 |
| SDD-0025-trajectory-session-store-and-serving | [文档](proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | JiuwenSwarm 轨迹存储与读取 API |
| SDD-0026-trajectory-observability-web-ui | [文档](proposals/SDD-0026-trajectory-observability-web-ui/design.md) | JiuwenSwarm Web 轨迹 UI |
| SDD-0029-team-session-runtime-integration | [文档](proposals/SDD-0029-team-session-runtime-integration/design.md) | JiuwenSwarm AgentRuntime / TeamManager 生命周期接入 |
| team_expert_group_manifest_design.md | [文档](archive/team_expert_group_manifest_design.md) | JiuwenSwarm Team 模式配置、装配或应用接入设计 |
| team_permission_design.md | [文档](archive/team_permission_design.md) | JiuwenSwarm Team 模式配置、装配或应用接入设计 |
| team_plan_design.md | [文档](archive/team_plan_design.md) | JiuwenSwarm Team 模式配置、装配或应用接入设计 |
| team_worktree_isolation_agenttool_cn.md | [文档](archive/team_worktree_isolation_agenttool_cn.md) | JiuwenSwarm Team 模式配置、装配或应用接入设计 |

另一仓库设计见 [设计索引](../../../../openjiuwen-develop-re/.agents/agent-docs/designs/INDEX.md)。跨仓相对链接按当前两个仓库同级放置；其他目录布局按稳定设计标识到相应仓库索引定位。
SDD-0009、SDD-0010、SDD-0028 中的历史 `agent_teams/workflow` 模块名属于 core；迁移不批量改写历史元信息。
SDD-0029 的历史 `modules: [agent_teams]` 与正文所有权不同，应用运行时归属以正文与源码为准。
SDD-0019 的 allocator/模型组协议、SDD-0022/0023/0024 的 core 观测框架、导出与投影，以及群聊 core 协议仍由 core 维护。

## 迁移验证

- 10 份设计与 4 张附件共 14 个文件，迁移前后 SHA-256 完全一致；原路径均已移除。
- core 的技能文件无修改；初次迁入时 swarm 的 10 个对应技能入口可加载，模板与支持文件随迁移补齐；随后按用户要求仅保留 5 个 `jw-` 技能。
- 两仓索引已重新生成：swarm 6 份标准设计，core 24 份；另 4 份迁入旧格式设计由 legacy-documents.md 定位。
- 初次迁入时实际运行报告解析脚本，Markdown、XLSX、pytest-html 的问题定位、已解决行过滤、traceback 和退出码验证通过；该脚本随后随非 `jw-` 技能移除。
- 实际运行索引工具，跨工作目录、跨仓模块前缀、多模块归组及文档去重计数验证通过。
- 迁移及技能引用未引入缺失的相对链接。core SDD-0017 中原有的 SDD-0015 相对链接仍有误，已与 HEAD 核对为既存问题，本次未改。
- 本次只涉及技能与文档，未运行业务测试、格式化或静态检查；未推送。提交记录以两仓 Git 历史为准。

## 技能范围调整

按用户要求，swarm 本次迁入技能仅保留 `jw-coding-flow`、`jw-design`、`jw-dev`、`jw-review`、`jw-testing`。
其余 5 个迁入技能及入口引用已移除。swarm 原有技能、迁入设计文档与 core 的技能保持不变。
