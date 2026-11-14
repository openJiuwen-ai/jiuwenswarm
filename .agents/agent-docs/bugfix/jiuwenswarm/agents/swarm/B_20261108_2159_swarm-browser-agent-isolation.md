---
id: B_20261108_2159_swarm-browser-agent-isolation
title: 团队浏览器子代理去重读取不存在的字段
type: B
author: yanhai
author_email: yanhai010@gmail.com
designs: []
design_note: 团队装配的既有声明式契约不变，只修正去重判据与用例，无独立 D。
modules:
  - jiuwenswarm/agents/swarm
files:
  - jiuwenswarm/agents/swarm/config_specs.py
  - tests/agents/swarm/test_swarm_assembly.py
---

# 现象与根因

`build_member_deep_agent_spec` 在把 base spec 里的浏览器子代理替换成按成员隔离的 `swarm.browser_agent` 时，用 `getattr(s, "subagent_type", None) == "browser_agent"` 判断条目身份。`SubAgentSpec` 没有 `subagent_type` 字段（已装 `openjiuwen 0.1.20` 的字段集为 `agent_card / system_prompt / tools / mcps / model / rails / skills / workspace / sys_operation / language / prompt_mode / enable_task_loop / max_iterations / factory_name / factory_kwargs`），pydantic 构造时直接丢弃该关键字。实测 `hasattr(spec, "subagent_type")` 为 False、`model_dump()` 也不含该键，因此两个判断恒为 `None`，移除分支从不命中。

`config_specs.py` 原注释称「team 模式 base spec 自带一个 server_id=playwright_official_stdio 的 browser_agent」。对该出处做了完整核查，结论是**当前版本不存在这样的注入路径**：

- 成员 base spec 只来自 `load_team_spec_dict`（`jiuwenswarm/agents/harness/team/config_loader.py`）后被 `TeamAgentSpec.model_validate` 解析。用仓库自带 `jiuwenswarm/resources/config.yaml` 实测，leader / teammate 的 `subagents` 均为 `None`。
- `_build_agents_config` 只是深拷贝 `modes.team.<id>.agents.<role>`（或它引用的顶层 `agents.<name>`）。实测在 `agents.teammate.subagents` 里显式写一条 browser 子代理，它会原样进入 base spec —— 这是唯一入口。
- 其余链路都不注入浏览器子代理：`resolve_deep_agent_parts` 只在 `add_general_purpose_agent` 为真时注入 general-purpose；`openjiuwen/harness/cli/agent/factory.py` 里那个会追加 browser spec 的工厂属 openjiuwen CLI，本仓团队路径不引用；AgentGroup 模板经 `apply_template_snapshot` 只挂 `agent_template_spec` 快照，其子代理在 `DeepAgentSpec.build` 时由 `extension_resolver` 物化，不进入 `base_spec.subagents`；`_normalize_team_identity_fields`、分布式 transport 规范化、`assemble_group_members`、design bridge 都不写 `subagents`；`interface_deep.py` / `interface_code.py` 里的 browser spec 属单 agent 适配器，不参与团队装配。
- 另核查 uv 缓存中的多个 agent-core rev，`SubAgentSpec` 从未有过 `subagent_type` 字段，故该判据自加入起就是死代码。

因此本缺陷的准确定位是「失效的去重逻辑」：默认配置下不产生用户可见故障；只有部署方在团队 `agents.<role>.subagents`（含 `$agent_*` 引用）里显式声明了浏览器子代理时，该共享条目才会与隔离条目并存，成员解析到共享实例、共用同一个 Chrome。修复后该前提下按 `agent_card.name` / `factory_name` 正常去重。

# 修复与不变量

- 新增 `_is_browser_subagent(spec)`，与同文件 `_is_explore_subagent` 同形：`factory_name` 命中 `{registry.BROWSER_AGENT, registry.SWARM_BROWSER_AGENT, "browser_agent", "browser_runtime"}`，或 `agent_card.name == "browser_agent"`，或外来 spec 对象自带 `subagent_type == "browser_agent"`。
- 工厂名先按 agent-core 物化时的归一方式（`str(...).strip().lower()`，见 `openjiuwen/harness/deep_agent.py`）处理后比较：`"  Browser_Agent  "`、`"Browser_Agent"` 这类写法在 core 侧仍会被实例化成共享浏览器，精确匹配会漏判，归一后一并命中。
- 去重分支与触发条件改用该谓词；顺序不变（先过滤 base，再 append 隔离条目与成员子代理），自身隔离条目不会被误删。
- 原注释把「团队 base spec 自带共享 browser_agent」写成无条件事实，与本次出处核查结论相反，已改成条件句并注明默认 `config.yaml` 不声明 base 子代理。
- `:1155` 处属于 explore 去重分支的注释保持原样，浏览器侧的载体说明写到新谓词与过滤分支旁。

# 验证

- `tests/agents/swarm/test_swarm_assembly.py::test_team_mode_deep_spec_replaces_shared_browser_agent` 重写为 5 种载体（agent card 名、`core.browser_agent`、`browser_agent`、`browser_runtime`、大小写与空白混写的 `"  Browser_Agent  "`）× 2 种模式（`team` / `code.team`）参数化，用真实 `SubAgentSpec` 构造共享条目，断言按对象身份（`shared_browser not in subagents`）、隔离条目恰好一个。修复前 10 条全红，修复后全绿。
- 新增 `test_team_mode_deep_spec_rebuilds_base_swarm_browser_agent`：base 条目本身已是 `swarm.browser_agent` 且带调过的 `factory_kwargs`，断言它被移除并以本平台默认参数重建——把「非等价替换」钉成可回归的行为。修复前红。
- `test_leader_deep_agent_spec_pins_subagents_empty` 去掉会被丢弃的 `subagent_type=` 构造参数。
- 该文件另有一处早已失效的 `from jiuwenswarm.common.mode_matrix import NEW_TEAM_DESIGN`（模块已随 gateway 拆分移除，文件第 65 行已从 `agent_gateway.common.mode_matrix` 导入同一常量），删除后整个文件才能被采集。
- `tests/agents/swarm/test_swarm_assembly.py` 195 passed；修复前基线 11 failed / 184 passed。`test_manifest_catalog` / `test_agent_group_assembly` / `test_skills_provider` / `test_electron_browser_fallback` / `test_team_kv_cache_affinity_config.py` 全绿。`test_group_chat.py` 与 `test_group_org_bootstrap.py` 运行耗时过长，未在其中执行；两者均无 browser / subagent 相关用例，不受本次改动影响。

# 明确不改

- `react.subagents.browser_agent.enabled: false` 时是否删除 base 里用户显式声明的浏览器条目，属另一项语义决定；默认配置的 base 无此条目，不受影响。
- 若 base 条目本身已是 `swarm.browser_agent`，仍会被移除并以本平台默认 `factory_kwargs` 重建，base 上调过的参数不保留；默认配置不出现此形态。该行为已由 `test_team_mode_deep_spec_rebuilds_base_swarm_browser_agent` 固定。
