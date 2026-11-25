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

## 当前群体记忆接线

沿用现有 `memory.builtin.provider=jiuwen`，团队的 Work 和 Code 模式使用同一 Team 场景。常规集群使用按账号归属派生的稳定主团队键，项目和会话只在各自的上下文中绑定。专家团按稳定团和模板身份绑定，临时成员使用 `main_agent`。SDK 使用原有文档 Runtime 持有者。

宿主标记真实用户原文，Core 将来源交给共享记忆 Provider。任务验收后的经验抽取由 Memory 统一完成。成员关系以 Memory 的持久登记为准。

常规集群与专家团在组装团队规格时登记名册，登记结果同步到成员恢复使用的 seed。Work 选中项目时刷新记忆绑定并登记成员，Code 在冷启动、完整 Spec 重载和 Rail 协调时补齐登记，成功后复用登记结果。管理租约在登记后释放。运行期 Provider 使用调用身份。登记失败记录错误，团队照常执行。显式加人成员库缺失时可初始化。

同一用户的画像支持跨 Agent 显式读取和修订，修订沿用 SUPERSEDE 并保留原条目归属。单人项目搜索同时考虑普通记忆与当前授权项目的群体条目，保留原工具的参数和返回格式。

SDK 与 HTTP 的成员管理复用 Provider 连接配置。注册的专用身份持有成员管理权，团长和专家按普通成员登记。HTTP 在 Memory 的可信主体注册表登记该专用身份，沿用原有网关密钥。显式专家登记、移出、模板删除和专家团卸载通过成员管理契约同步；停止会话保留持久名册。Rail 安装和显式成员变更共用同一模板身份解析。Team Provider 实例固定项目和群体坐标，SDK 租约调用使用自身的绑定；同一会话内切项目及撤权失败后的补偿留待后续。

Swarm 的 agent_group_name 仅在宿主侧用于派生专家团的稳定 team 键和解析成员模板身份（包括 role="leader" 对应的 {agent_group}:leader），不再作为 group 维度传给 Memory 或登记到 members.db（Memory 内核保留 group 维度能力供后续独立协作组使用）。

## 上游基线决策与装配

按原设计 D-08、5.7 和 V-15，memory.builtin.provider=jiuwen 即启用 team.work 与 team.code 的文档记忆，由 scene=team 工厂创建每位回答者独立的 JiuwenTeamMemoryRail/Provider。不读取旧团队 modes.team.*.memory.enabled、单人 modes.agent/code.memory.enabled 或 memory.mode/engine 作为启用条件；旧开关为 false 或缺省均可启用。仍需宿主绑定团队坐标及有效的 SDK/HTTP 配置；启用 Jiuwen 但缺少团队坐标时记录告警并跳过 Rail，不回退旧记忆。默认 provider 不改，team.design 和原生路线保留既有行为，单人开关不变。此前实施中的单人开关及 mode/engine 限制属于偏离原设计，本次按已确认要求移除。

按 2026-11-18 确认的统一方案，Work/Code/Team 的 Jiuwen 记忆身份只取 memory.builtin.jiuwen.agent_id，缺省为 main_agent；运行名称和角色不变。此决定替代此前“入口名称优先、再取配置”的规则，同配置的自定义助手也共享记忆。项目使用现有绝对路径摘要键，无项目为 default。团队键使用 org/user、项目、专家团的稳定摘要，不含聊天 ID 或成员名。三个坐标进入可序列化 SwarmBuildContext；恢复时配置身份优先于旧 seed，项目和团队键继续保留。SDK 媒体授权和配置重建指纹使用同一身份解析；只改运行名称不再触发记忆身份切换。

团队入口不保留单人记忆轨和 ProjectMemoryRail；成员不装旧 TeamMemoryManager/CodingMemoryRail。编码回答者上的 ProjectMemoryRail 可继续存在。冷启动与模式切换使用同一 Team 判断：进入 Team 卸载入口说明书轨，返回单人 Code 按原开关恢复。Jiuwen 失败不回退原生记忆，正常对话继续。

## Runtime 所有权

SDK Work/Code/Team 共用 document_runtime_holder，规范文档根决定 Runtime；各 Provider 保留自身身份和坐标。首份配置生效，后续差异只告警。builder 的 shared_document_root 参数仅为兼容旧调用保留，SDK 始终提供共享根目录，HTTP 忽略该参数。持有者使用持久事件循环启动看门狗和运行请求；鉴权上下文每次在调用线程取得，不能复用首位 Provider 的身份。

租约构造成功后才增加引用计数；首次身份绑定失败时清理无人持有的实例，借用已有实例失败不影响原持有者。正常关闭期间的新申请等待关闭完成后重新查找实例，多个等待者共用一个新实例；等待时释放宿主线程锁，其他根目录可正常操作。取消调用方不取消已接受操作，最后释放排空后关闭；同一 Runtime 的 Portal 注册共享运行标识，各 Provider 保留自己的授权和句柄；关闭失败隔离旧实例，禁止覆盖。进程退出做兜底清理。HTTP 不进入本地池；SDK continuity 的独立池及其冲突契约不变。

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

## 2026-11-23 CI 修复

UT 分层检查发现 common/team_memory_binding.py 反向依赖 agents 下的装配模块。provider 的纯配置读取移至 common/memory_config.py，Team 判断与 builder 共用；builder 原入口和未传配置时的加载行为保留，Team 启用条件不变，不添加分层白名单。

DocumentRuntimeHolder 的关闭锁和工作循环仍由 holder 持有；租约通过明确方法跟踪调用、等待完成和幂等关闭，holder 通过只读属性访问资源坐标。消除两个类之间的私有成员访问并补齐嵌套定义空行，保持提交/关闭共用同一锁、调用取消不撤销已接受写入、最后引用排空后关闭及关闭失败隔离的契约。

在修改前复现 CI 同一分层失败。修复后运行该失败用例及前述五个相关测试文件，共 358 项通过；新增覆盖显式空/异常配置不触发默认加载，以及关闭期间拒绝新调用、已有写入完成和重复关闭仅释放一次资源。使用 Python 3.12、本地 Swarm/Core/Memory worktree 与本地 Gateway/E2A 源码，模型为确定性替身；未重跑云端 CI、浏览器或真实 LLM。本次为既有 Team 特性的实现修正，不变更已确认的共享、隔离或发布依赖方案。


## 2026-11-23 PR 检视修复

修复正常关闭窗口的新申请失败和租约绑定失败导致的引用泄漏；关闭异常继续隔离原根目录，禁止替代实例覆盖。缺失团队坐标时新增告警，禁用及非 Team 路线不产生该告警；补齐 TEAM_MEMORY 导出，明确 shared_document_root 兼容参数的实际含义。

修复前用四个回归场景复现：关闭中的新申请失败、新建实例绑定失败未清理、已有实例借用失败后引用泄漏，以及缺少团队键时没有告警。新增测试复用 Resource 夹具，验证正常关闭后的并发领取只创建一个替代实例，等待时其他根目录仍可读写；已有关闭失败隔离测试继续保留。视频目录登记、视频工具类型识别、模型热更新及旧 seed 跨身份恢复不在本次修复范围。

本次定向回归 14 项通过；扩大至原 CI 分层失败用例和五个记忆相关测试文件，共 361 项通过（24.56 秒）。当前 Core worktree 缺少 Swarm 消费的 DiscoverySession，两个入口测试在收集阶段失败；扩大回归改用此前兼容的 Core 提交 c6b63dc0d 源码快照，其余使用当前 Swarm/Memory worktree 和本地 Gateway/E2A 源码。未修改 Core 分支或依赖声明，不能将快照结果视为当前 Core worktree 的兼容性通过。Ruff、嵌套定义空行、行长、复杂条件及跨类私有成员访问检查相对修改前没有新增告警；未重跑云端 CI、浏览器或真实 LLM。

## 2026-11-23 主线对齐后的 Core 依赖升级

Core 依赖统一固定到 8e9e6df5b2af3b1d4d3fcf72bdb359ff7efa9f26（0.1.21），直接依赖、各 extra 的 override 和 uv source 保持一致。uv 0.11.14 重新生成锁文件，新增 markdownify 1.2.4、xlrd 2.0.2 和 pyxlsb 1.0.10；python-pptx 已存在，仅补入 Core 的依赖关系，其他既有包版本和来源不变。仓库内 `uv lock --check --offline` 通过，共 346 个包。

主线对齐后已包含 Symphony 接口及事件总线适配。在 Python 3.12 下，使用目标 Core 精确源码、当前 Swarm/Memory 源码及锁定的 Gateway/E2A 源码，运行上述五个记忆测试文件、`tests/unit_tests/extensions_v2/test_team_extension_wiring.py` 和 OpenTelemetry 依赖约束用例：374 项通过，1 项失败。Team 工作/编码 Rail 的真实 SDK 写入与召回、统一身份、项目/团队隔离、Runtime 生命周期、配置重载和事件总线接入均通过；此前的 DiscoverySession 导入与 create_session 调用错误未再出现。

剩余失败为 `test_swarm_skill_retrieval_prompt_uses_same_live_inventory[connector]`，测试期望返回空 Rail，实际创建了技能检索 Rail。该断言已在未修改的主干及其原锁定 Core 上单独复现，本次不修改该主干问题。目标 Core 自带的 HTML、XLS、XLSB、PPTX 等格式解析用例另有 7 项通过，新增解析依赖仅安装在临时目录。

本次验证使用确定性模型和本地 Memory 实现，未更新共享虚拟环境、未验证仅安装已发布 JiuwenMemory 包的完整部署，也未重跑浏览器或真实 LLM；不能据此宣称全量 CI 或发布环境全部通过。


## 2026-11-23 共享 Runtime 的视频上传目录授权

按已确认的最小修复方案，仅修改 Swarm：在配置为完全访问且媒体配置存在时，首次 SDK 装配创建并授权当前用户／租户的稳定 sessions 父目录，绑定既有五维记忆身份。后续新增会话目录无需更新 Runtime；Work/Code/Team 的初始装配共同透传媒体配置。此修复承接前述 PR 检视中暂缓的视频目录问题，不替代首份配置生效的 Runtime 所有权规则。

允许同一记忆身份导入该 sessions 子树内各会话的合法视频文件；这是一项明确的授权范围扩大，不将项目记忆过滤解释为原始媒体文件隔离。现有显式部署目录、Work 本地/沙箱及权限门禁保留，不扩展到其他用户／租户根。没有新增复制、清理任务、视频工具或依赖；未选择固定中转目录方案，避免额外磁盘复制和异步任务副本生命周期。

已有 Runtime 的模型、权限与身份热更新仍不支持；首次装配须具备有效配置。修改后需释放全部借用者重建或重启。Memory 侧既有的租约能力透传修复仍为视频工具可用的前提，本次未修改 Memory/Core 仓库。

验证：修复前 Work/Code/Team 三种首次装配场景均因缺少稳定目录绑定失败；修复后，在真实 SDK、文件授权、任务队列、Markdown/影子索引及真实看门狗下验证三种初始化顺序，后创建的两个会话均可导入视频，Provider 共用同一 Runtime，watchdog 仅启动一次；临时副本完成后清理，目录外文件和错误 Scope 被拒绝。只替换视频模型流水线输出，未调用真实视频/ASR 模型或浏览器。

在 Python 3.12、目标 Core 8e9e6df5 源码和当前 Memory worktree 下，运行既有五个记忆回归文件，加 `test_user_turn.py` 与 `test_media_capability_config.py`，共 412 项通过、1 项失败（22.09 秒）。唯一失败仍为已在未修改主干复现的技能检索 `test_swarm_skill_retrieval_prompt_uses_same_live_inventory[connector]`，本次未修改该逻辑。最后重跑构建器与 Runtime 两个定向测试文件，96 项全部通过（16.78 秒）。Python 语法及 `git diff --check` 通过；遵循仓库约束未运行格式化/静态检查，未重跑远端 CI。

## 2026-11-23 Rebase 主干后的依赖对齐

当前 Core 依赖随主干固定到 02b2ce4538b60cc4fd6085db36021b48ee5a1d99；此前 8e9e6df5 的记录保留为历史验证基线。已核实前者包含后者，且两者记忆 Rail 目录内容一致。依赖声明与锁文件完整保留主干版本，保留 Symphony 新能力及 openjiuwen-runtime-foundation 0.1.2b1；不逐块拼接锁文件，避免自动合并留下 markdownify、xlrd 的同版本重复记录。Team Provider、Runtime 复用和稳定视频目录授权实现保持不变。

`uv lock --check --offline` 通过，共 338 个依赖包。在 Python 3.12 下，使用主干精确 Core 源码、当前 Memory worktree 和锁定的 Gateway/E2A 源码，重跑前述七个记忆相关测试文件，415 项全部通过（31.40 秒）；此前技能检索 connector 断言已由主干更新，本次回归通过。18 个 Python 文件语法及差异空白检查通过。未修改共享虚拟环境，未运行格式化或静态检查工具，也未重跑浏览器、真实模型、全量 CI 或仅使用发布包的部署验证。
