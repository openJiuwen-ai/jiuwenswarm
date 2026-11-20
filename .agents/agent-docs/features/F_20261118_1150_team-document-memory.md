---
author: Wang Tao
author_email: zzyyww0808@gmail.com
id: F-20261118-1150-team-document-memory
title: Team 接入 Jiuwen 文档记忆
type: F
modules: [jiuwenswarm/agents/swarm, jiuwenswarm/agents/harness/common/memory, jiuwenswarm/server/runtime/agent_adapter]
files: []
designs: []
design_note: 依据 D-20261113-2017-jiuwen-memory-team-upgrade 及已确认的跨仓 Team 方案。
---

# Team 接入 Jiuwen 文档记忆

日期：2026-11-18。

## 决策与装配

按原设计 D-08、5.7 和 V-15，memory.builtin.provider=jiuwen 即启用 team.work 与 team.code 的文档记忆，由 scene=team 工厂创建每位回答者独立的 JiuwenTeamMemoryRail/Provider。不读取旧团队 modes.team.*.memory.enabled、单人 modes.agent/code.memory.enabled 或 memory.mode/engine 作为启用条件；旧开关为 false 或缺省均可启用。仍需宿主绑定团队坐标及有效的 SDK/HTTP 配置。默认 provider 不改，team.design 和原生路线保留既有行为，单人开关不变。此前实施中的单人开关及 mode/engine 限制属于偏离原设计，本次按已确认要求移除。

按 2026-11-18 确认的统一方案，Work/Code/Team 的 Jiuwen 记忆身份只取 memory.builtin.jiuwen.agent_id，缺省为 main_agent；运行名称和角色不变。此决定替代此前“入口名称优先、再取配置”的规则，同配置的自定义助手也共享记忆。项目使用现有绝对路径摘要键，无项目为 default。团队键使用 org/user、项目、专家团的稳定摘要，不含聊天 ID 或成员名。三个坐标进入可序列化 SwarmBuildContext；恢复时配置身份优先于旧 seed，项目和团队键继续保留。SDK 媒体授权和配置重建指纹使用同一身份解析；只改运行名称不再触发记忆身份切换。

团队入口不保留单人记忆轨和 ProjectMemoryRail；成员不装旧 TeamMemoryManager/CodingMemoryRail。编码回答者上的 ProjectMemoryRail 可继续存在。冷启动与模式切换使用同一 Team 判断：进入 Team 卸载入口说明书轨，返回单人 Code 按原开关恢复。Jiuwen 失败不回退原生记忆，正常对话继续。

## Runtime 所有权

SDK Work/Code/Team 共用 document_runtime_holder，规范文档根决定 Runtime；各 Provider 保留自身身份和坐标。首份配置生效，后续差异只告警。持有者使用持久事件循环启动看门狗和运行请求；鉴权上下文每次在调用线程取得，不能复用首位 Provider 的身份。

领取/归还按引用计数控制；取消调用方不取消已接受操作，最后释放排空后关闭；同一 Runtime 的 Portal 注册共享运行标识，各 Provider 保留自己的授权和句柄；关闭失败隔离旧实例，禁止覆盖。进程退出做兜底清理。HTTP 不进入本地池；SDK continuity 的独立池及其冲突契约不变。

## 共享与内容限制

同身份下 USER.md 共享；同项目的事实在单人与团队、Work 与 Code 间共享；团队结论只在同项目和专家团共享。成员名、侧栏只记录来源。新 write/edit 工具加入 MemoryForbiddenRail，自动 sync_turn 和团队条目写入也复用已有敏感内容检测；开关关闭时沿用原策略。

## 拒绝的方案

不改运行 Agent 名称，不硬编码不可配置的记忆身份，不使用会话 ID 做团队键，不在入口多创建一个 Provider，不用资源锁替代 Runtime 生命周期管理。不改 AutoMemory、Dreaming，不迁移旧文件，不做跨进程协调或复杂语义去重。旧身份数据不自动归并或跨身份搜索；默认 main_agent 与原默认写入身份对齐；显式配置其他 agent_id 时尊重该值，不自动覆盖。

## 验证与交付

test_swarm_assembly.py 覆盖真实 TeamAgentSpec 装配、身份/种子传播、独立成员 Rail、原生/Design 路线；test_team_memory_runtime.py 覆盖首份配置、取消写入、创建失败、独立根目录及真实 SDK 共用 Runtime。公共工厂 + Core Runner + Memory SDK 测试使用确定性模型，检查模型实际上下文及最终 Markdown。Portal 多 Provider 共用、启动失败重试和关闭失败隔离也有回归。三仓联调通过 worktree 源码路径加载，不代表已发布依赖已包含新接口；发布时须更新配套 Core 和 JiuwenMemory 版本。

离线确定性测试覆盖接线和副作用；真实 LLM 质量、浏览器页面及跨进程部署仍需独立验收。

验证命令：`pytest -q -p no:cacheprovider -o addopts= tests/agents/swarm/test_team_memory_runtime.py tests/agents/swarm/test_swarm_assembly.py tests/unit_tests/agentserver/memory/test_jiuwen_rail_builder.py tests/unit_tests/agentserver/memory/test_code_adapter_coding_memory.py tests/unit_tests/agentserver/memory/test_jiuwen_coding_integration.py`。使用三仓源码和当前锁定的本地 E2A/Gateway 源码依赖；共 310 项通过（15.82 秒）。覆盖 SDK/HTTP 装配、配置身份切换、旧 seed 恢复和媒体 Scope；真实 SDK 验证不同运行名称下画像共享、项目与团队隔离、不同用户及配置 Agent 身份不可见。上游 Spec 重载测试夹具补齐 extension_bus。

启用规则回归先复现旧开关关闭时 Team 工厂返回空值，再验证修复。覆盖团队及单人开关为 false/true/缺省、memory.mode=cloud 与 engine=external，Leader/成员及 seed 恢复仍装配 Team Rail；provider 为空、team.design、单人模式和未绑定团队坐标时不装配。真实 Team 工厂 + Core Runner + Memory SDK 在工作、编码两种侧栏下均验证了四个工具注册、模型上下文召回和团队结论落盘；模型为确定性替身。本次未重跑浏览器页面或真实 LLM。

四项检视修复后的本仓回归：313 项通过，覆盖 Team 装配、共享 Runtime、公共 builder、Code 入口及 Coding 集成。测试使用离线模型及本地三仓源码，未重跑真实 LLM 或页面验收。

## 2026-11-20 主线对齐验证

复用主干已合入的 Coding 实现，仅迁移 Team 增量。主干模板默认 provider 已为 jiuwen，原生编码成员装配用例显式选择空 provider，继续验证原生 CodingMemoryRail 的声明式装配；业务启用规则保持不变。文档索引按生成规则登记 Team 条目，保留主干已有条目。

使用当前 Swarm、Core、Memory worktree 源码及本地 E2A/Gateway 源码，前述五个测试文件共 351 项通过。覆盖 Team 装配、Runtime 复用、身份与隔离、SDK/HTTP builder、Code 入口和原生路线；测试使用确定性模型，未重跑浏览器或真实 LLM 验收。
