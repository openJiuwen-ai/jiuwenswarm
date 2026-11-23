# OfficeAce 会话的技能目录提示与实际加载目录不一致

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-21 |
| 范围 | RuntimePromptRail、身份提示、DeepAdapter 装配及对应回归测试 |
| 关联 spec | `../specs/S_25_runtime-skill-directory-context.md` |
| 测试基线 | 本地 `.venv`，原基线 75 个测试通过，合并上游后 99 个测试通过 |

## 现象与证据

2026-11-20 的 `officeclaw_c25804b209e4d931e916cb24` 会话安装 `skill-turbo-converter`。官方安装工具返回回调配置错误后，助手将技能复制到 C 盘的 `jiuwenclaw_workspace/skills`，并写入 `skills_state.json.local_skills`。最终 `skill_tool` 返回 `Skill not found`。

该会话的 `history.json` 第 28、43、109、114 行分别记录目录查看、复制、登记和加载失败。`full_20261120_152607.log` 第 25758 行记录模型提示中的 C 盘“Agent 技能目录”，第 25478 行记录加载组件实际使用的 E 盘产品及用户技能目录。

## 根因

`RuntimePromptRail._format_agent_data_paths` 将内部存储目录称为“Agent 技能目录”。`SkillUseRail` 的目录由适配层按共享目录、企业预置及扩展配置解析。两处各自描述同一技能环境时，提示缺少当前加载位置，安装工具报错后的手工复制使用了内部存储路径。

## 决策

- 内部存储路径继续使用原有路径函数，并明确命名为“JiuwenSwarm 内部技能存储目录”。
- DeepAdapter 提供当前 `SkillUseRail.skills_dir` 的读取回调，每次模型调用读取当前组件，覆盖重建及扩展目录。
- 中英文静态提示同步目录语义。OfficeAce 正常安装说明明确由宿主工具选择目标目录并完成注册。
- 本次按用户确认实施目录来源和目录说明两项修改。

## 拒绝的方案

将 `get_agent_skills_dir()` 全局改成宿主共享目录会改变内部账本和其他存储消费者的路径语义。采用独立展示的加载目录可以保留这些消费者的现有行为。

将共享列表的第一个目录作为安装目的地会选择产品或企业资源根目录。安装目的地继续由已有宿主安装工具管理。

## 验证

新增 8 个参数化用例在实现前全部失败，失败涉及内部目录名称与加载目录缺失。用例通过真实 DeepAdapter 装配和 `SkillUseRail` 构造，检查最终生成的中英文提示，并覆盖组件替换及移除。

修复后使用仓库 `.venv` 执行以下三个文件，结果为 75 passed、1 个第三方 Authlib 弃用警告。

```powershell
$env:PYTHONPATH = (Get-Location).Path
.venv/Scripts/python.exe -m pytest tests/unit_tests/agentserver/test_system_prompt_restructure.py tests/unit_tests/agentserver/test_projectless_agent_workspace.py tests/unit_tests/agents/harness/common/rails/test_runtime_prompt_rail_event_loop_stall.py -o addopts='--asyncio-mode=auto --tb=short -q' -o log_cli=false
```

相关验证覆盖最终生成提示、无项目工作区、PathProvider 记忆目录配置以及慢盘事件循环响应。加强企业预置场景的共享目录对照后，新增 8 个用例再次通过。测试使用本机已安装的 openjiuwen 依赖，验证结果限定于该环境。

### 合并上游后的复验

2026-11-21 合入目标分支 `dev-stable` 的 `da61667dc`，处理 PR #10733 的两份文档索引冲突。索引同时保留上游的 RSI、DeepResearch 日志与技能目录说明。本规约重新编号为 `S_25`，本故障记录重新编号为 `B_08`，相关引用同步更新。

在上述三个测试文件基础上增加 `test_flash_prompt_snapshot.py` 和 `test_flash_persona_rail.py`，使用相同 pytest 参数执行，结果为 99 passed、1 个第三方 Authlib 弃用警告。

### 行评后的提示修订

2026-11-22 根据 PR #10733 行评继续修订同一问题。中文技能表格行原有 136 个显示列，英文长行已在前次整改中拆分。本次将中文行续写为两行，中英文操作建议改为仅在运行时提供加载目录时使用该信息，避免未传目录回调的团队入口出现无对应条目的指引。

技能组件为空时，原提示“未启用”会把未启用、装配失败等原因混为一谈。本次改为“暂无可用目录”及英文 `none available`，仅描述当前可用信息。三个团队入口的回调装配保持现状，不新增失败原因跟踪或安装失败处理策略。文档索引的“建档批次”同步改为类别“技能目录提示”。

更新已有身份提示和组件移除测试后，修复前四个定向用例因旧提示不符合预期而失败。修复后执行前述五个测试文件，结果为 99 passed、1 个第三方 Authlib 弃用警告。中文续行按东亚宽字符计两列分别为 60、65 列，英文三行分别为 87、47、55 列，均不超过 120 列。未运行格式化或静态检查工具，平台门禁结果仍以新提交对应的 CI 为准。

## 已知遗留

该会话使用的 Relay 旧 MCP 编译产物与源码不同，官方工具的回调报错需要在对应构建和启动环节处理。本次验证范围为提示内容与装配行为，真实模型安装结果需要部署后复测。安装失败后的处理提示按用户要求留待后续工作。
