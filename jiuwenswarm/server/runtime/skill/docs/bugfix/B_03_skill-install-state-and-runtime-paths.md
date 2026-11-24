# 卸载后重新请求安装时，历史状态与工作区目录导致误报已安装

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_03 / skill-install-state-and-runtime-paths |
| 日期 | 2026-11-24 |
| 范围 | `agents/harness/common/rails/runtime_prompt_rail.py`、`agents/harness/flash/prompt/content/{tools,persona}.py` 及对应测试 |
| 关联 spec | [S_05_agent-skill-rails.md](../specs/S_05_agent-skill-rails.md) |
| 测试基线 | 新增用例在修复前 8 项失败；修复后相关回归 323 项通过（含新增 9 项） |

## 现象与证据边界

OfficeAce 中首次安装 `ppt-master`，用户在界面卸载后，在同一会话再次请求安装，智能体仍回复已安装。
依据用户提供的 `log_20261123_152506_20261123_160914.zip`，会话历史中可核对：

- 15:39 的管理查询包含目标技能，`availableToAgent: true`。
- 15:41:54 运行时重建后的可用清单已移除目标技能；15:42 的答复仍沿用旧结果。
- 15:44 目标请求仅执行两次 shell 调用，没有重新查询或安装。`history.json` 第 63 行记录其因“上轮已查过同样参数”跳过查询的理由。
- 第 71 行检查的是 C 盘 Agent 工作区技能目录，返回 13,018 个文件；第 74 行据此及历史查询结果断言已安装。正式技能发现根目录包含 D 盘的共享技能目录，两者并非同一处。

同日志包其他请求的运行时提示将该 C 盘路径标为“Agent 技能目录”，与目标轮记录的路径选择理由一致；目标轮完整系统提示未取得。
工作区目录的来源、本次卸载请求的完整结果尚未查明，不能据此认定 relay 卸载仅删注册或漏删目标安装目录。

## 根因与修复决策

1. 运行时目录提示按工作区拼出 skills 路径，而加载入口按实际 `SkillUseRail` 配置发现技能，给模型提供了不一致的路径含义。
2. Flash 去重文案未突出本次用户请求的作用范围，模型将历史查询当作当前状态依据。

当前实现沿用上游已合入的 `skill_dirs_provider`：Adapter 注入每次现读 `self._skill_rail.skills_dir` 的回调，`RuntimePromptRail` 在模型调用前获取当前加载目录。加载器被替换或禁用后不复用旧目录；提供者返回空列表时显示暂无可用目录，不回退到工作区存储路径。

提示使用上游统一命名“JiuwenSwarm 内部技能存储目录”和“当前会话技能加载目录”，只输出一套加载目录，并补充目录存在不证明安装或可用状态的提醒。工作区存储路径保持原有 PathProvider 语义，OfficeAce 的宿主安装工具指引保持不变。

初版曾在共享技能协议中加入四条安装状态规则；该部分已按用户要求撤回，当前仅保留目录提示与去重规则的修改，撤回依据见下文。
同步更新 Flash 工具去重范围，保留重复调用守卫。未加入“加载失败后处理”、强制重装或名称冲突的新规则。

## 初版回归证据与适用范围（含已撤回部分）

环境：Windows，仓库本地 `.venv`，设置 `PYTHONPATH` 为仓库根目录，openjiuwen 0.1.16。
当前 `.venv` 的 openjiuwen 安装来源提交与仓库 `uv.lock` 固定提交不一致；本次未修改依赖锁，也未重建锁定依赖环境。

新增 `tests/unit_tests/agentserver/test_skill_install_state_prompt.py`：

- 使用真实 DeepAgent 的公开 rail 添加/移除接口及真实 SkillUseRail/TeamSkillUseRail，观察最终 `SystemPromptBuilder.build()` 文本。
- 构造与正式库分离的工作区残留文件，验证发现根目录不误用工作区；覆盖中英文、字符串/列表根目录、替换后旧路径消失、移除后显示未知、不同宿主目录隔离和工作区作为真实加载根目录的场景。
- 通过共享协议注入及 FlashSkillRail 实际抽取，确认四条安装规则进入最终加载段；通过 Flash 声明式组装确认去重规则在中英文中生效。
- 修复前新增 8 项用例因缺少目标路径/规则失败；加入隔离场景后新增共 9 项通过。

执行命令（仓库根目录，已激活 `.venv` 并设置 `PYTHONPATH`）：

```powershell
python -m pytest tests/unit_tests/agentserver/test_skill_install_state_prompt.py tests/unit_tests/agentserver/test_system_prompt_restructure.py tests/unit_tests/agentserver/test_flash_prompt_tools_content.py tests/unit_tests/agentserver/test_skill_protocol_extraction.py tests/unit_tests/agentserver/test_flash_skill_rail.py tests/unit_tests/agentserver/test_flash_skill_protocol_rail.py tests/unit_tests/agentserver/test_flash_repeat_call_guard_rail.py tests/unit_tests/agentserver/test_flash_prompt_offline.py tests/unit_tests/agentserver/test_flash_prompt_data_plane.py tests/unit_tests/agentserver/test_flash_prompt_composer_rail.py tests/unit_tests/agentserver/test_flash_prompt_audit_rail.py tests/agents/swarm/test_swarm_assembly.py -o addopts="--asyncio-mode=auto" -q --tb=short
```

结果：323 passed，1 个既有 Authlib 弃用警告。原有目录测试改为分别断言存储目录、发现目录及无加载入口时的信息边界。
原有重复调用守卫的新请求重置、只提醒不否决等测试继续通过。

## 资料同步与遗留

- 当前目录事实源及去重范围写入关联 S_05；四条共享协议安装规则已撤回。开发记录集中在本文，不新增重复台账。
- 这些测试证明目录展示与提示词装配契约，不证明真实模型一定遵循规则。尚未进行 OfficeAce 真机/真实模型的“安装→界面卸载→同会话再安装”回归，也未验证其安装副作用。
- 用户日志中的工作区技能目录来源与清理归属仍待调查；不属于本次修复。

## 2026-11-24 撤回四条安装状态规则

用户要求删除共享协议中新增的四条中英文安装状态规则，恢复该文件修改前的内容；实际发现目录提示、目录存在不代表已安装的提醒及工具去重修改继续保留。

11:38 会话 `officeclaw_c18240c4418212f692620573` 的完整模型请求表明：目录提示和去重修改已进入系统提示，四条共享协议规则未进入。当前 Flash 装配不保留 `SkillProtocolPromptRail`，也没有实例化旧 `FlashSkillRail`；初版测试直接调用这两个组件，未覆盖真实 Flash Adapter 装配，不能证明四条规则在该入口生效。本次撤回同时移除该规则抽取测试的两个语言用例，并从 spec 删除对应安装规则约束。

初版 323 项通过的结果保留为历史记录，不作为撤回后的验证结果。

撤回后验证：共享协议文件内容与当前 HEAD 完全一致，relay vendor 通过指向本仓的目录链接同步恢复。运行 `test_skill_install_state_prompt.py`、`test_system_prompt_restructure.py`、`test_skill_protocol_extraction.py`、`test_flash_skill_rail.py`、`test_flash_skill_protocol_rail.py`、`test_flash_prompt_tools_content.py`，结果为 82 passed、1 个既有 Authlib 弃用警告；未执行真实模型回归。

## 2026-11-24 合并上游目录修复并解决 PR !10834 冲突

上游 `dev-stable` 已通过 !10733 合入 `skill_dirs_provider` 目录提供机制及 Adapter 回归。原 PR 与上游同时修改了中英文存储目录标签和多租户测试断言，且自动合并会保留两套目录读取和展示逻辑。

处理方案：沿用上游提供者、标签及安装指引，移除本 PR 的 `find_rails_by_type` 读取与重复目录块；保留目录存在性提醒和 Flash 工具去重修改。目录测试复用上游真实 Adapter 入口，增加工作区残留文件、加载目录只展示一套及中英文状态核实提醒的断言；删除直接手动注册加载 rail 的重复测试，独立测试文件仅保留去重规则用例。

在加入提醒前，六项 Adapter 参数化用例均因缺少对应提醒失败；这证明现有上游路径能够复现本次补充项的差异。

合并后在仓库 `.venv` 中运行上文列出的同组 12 个测试文件（覆盖目录提示、协议抽取、去重守卫、Flash 提示词组装和 Swarm 装配），结果为 330 passed、1 个既有 Authlib 弃用警告。测试覆盖目录来源与提示装配，不证明真实模型必然遵守；本次未执行 OfficeAce 真机回归。
