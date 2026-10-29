---
id: F_20261026_2050_skillpack-workflow
title: SkillPack 沉淀 Tool 与直达执行实施记录
type: F
author: 霆龙(吴建宇)
author_email: wujianyu@xiaohongshu.com
modules:
- jiuwenswarm/agents/harness/common
- jiuwenswarm/symphony
- jiuwenswarm/server/runtime/agent_adapter
files:
- jiuwenswarm/agents/harness/common/tools/skillpack_toolkit.py
- jiuwenswarm/agents/harness/common/rails/symphony/skillpack_route.py
- jiuwenswarm/symphony/skillpack_candidates.py
- jiuwenswarm/symphony/service.py
- jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
designs:
- symphony-design-docs:SDD-0013
design_note: 依据 symphony-design-docs 仓库 proposals/SDD-0013-jiuwenswarm-skill-pack-productization/design.md（v9）；设计仓库：https://gitcode.com/lin-xiaoyu/symphony-design-docs。
---

# SkillPack 沉淀 Tool 与直达执行实施记录

## 关联依据

- 设计：SDD-0013 v9 §4.2.1、§4.2.2。
- 规约：[SkillPack 沉淀与直达执行](../../../../../specs/jiuwenswarm/agents/harness/common/S_01_skillpack-workflow.md)。
- 日期：2026-10-26。
- 范围：对话 Tool、服务端证据归属/确认、Symphony 编排前路由及执行约束；不改 Core 通用 SkillRecord，不包含 Hub 原生后端。

## 实际实现与开发决策

新增 `SkillPackToolkit`，通过现有 `SymphonyToolkit` 装配，复用 `SwarmSymphonyService` 与 Core Flow。真实执行图在宿主端读取当前普通成员类别，并将 trace 的用户/session 归属及成员内容指纹持久化。Tool 只能选择已存在的证据和候选，不能提交自造图或成功率；生成和安装分离，使用现有 Host 问题展示并确认。

用户作用域支持同用户跨会话聚合。不能确认用户身份时收窄到当前会话；成员当前内容必须与证据快照一致。没有直接给 Core 配方补用户字段，而在宿主存储中保留身份边界，避免将 JiuwenSwarm 用户模型注入通用 SDK。

确认时保存服务端制品摘要及成员指纹，安装时重验。类型以根文档及普通 Skill 标记判定，不把 `capability_type=skill` 当成普通成员的证明。Core 新的普通成员检查与本端 Tool 需配套使用；运行旧 pin 时在候选准备返回 `core_skillpack_contract_missing`。Tool 注册受 Symphony 和产品化开关双重控制，ABI 检查发生在调用时。

新增 `SkillPackRouter` 并由 `interface_deep.py` 注入真实 manager、检索及请求模型；`SymphonyOrchestrationRail` 保存请求内选择和执行边界。自动完整匹配先确认，Picker 单选可直接确认，混合或多个候选明确选择。路由 token 由宿主创建，模型文本或参数不能设置；工具执行仍经过原权限机制。

不把 token 写入 checkpoint。直达已执行成员后如果内存授权丢失，恢复旧输入不能安全推断哪些副作用已发生，所以 checkpoint 只保存拒绝标记，要求重新选择。首个根加载检查真实 ToolOutput 的成功状态和根文件路径，失败或读取附属文件不算根加载。

检视发现旧经验 API 仍能无作用域读取和确认全局候选。产品化开启时封闭旧 list/request/安装 ID，并保留关闭时的旧实验行为；不能仅保护新 Tool 而留下旧入口旁路。另一处时间窗发生在异步评审期间：初始成员快照通过后，成员仍可能变化。最终提交前增加共享库存锁内的第二次快照/启用校验，并同步完成恢复与安装；评审等待本身不占用库存锁。

## 与设计的偏差和限制

沉淀继续使用现有 Host 问答，不新增独立候选编辑器。当前检查只识别新 Core 的最小接口能力，不替代最终打包依赖验证；仓库 agent-core pin 尚未自动更新。

自动直达使用当前本地检索及结构化模型判定，召回不足时进入原编排；不会自动把不确定结果当成匹配。真实模型的召回和覆盖判断质量需集成环境评估。早期实现对全局配方做事后归属检查，混合会话记录会隐藏原有候选；该问题已改为聚合前按作用域过滤，见下文修复记录。

## 实际执行与证据

测试使用新 Swarm/Core 工作树源码及 Python 3.11.6，复用只读的 `symphony-integration/work/.venv/bin/python`。所有 pytest 均禁用仓库默认附加参数；未运行格式化或静态检查。

2026-10-26，沉淀相关组合 **119 项通过**（新增评审时间窗修复前）：

```bash
PYTHONPATH=/Users/wujianyu/Documents/layshang/Projects/skillpack-productization/work/jiuwenswarm:/Users/wujianyu/Documents/layshang/Projects/skillpack-productization/work/agent-core /Users/wujianyu/Documents/layshang/Projects/symphony-integration/work/.venv/bin/python -m pytest tests/unit_tests/symphony/test_skillpack_tool.py tests/unit_tests/symphony/test_startup_singleflight.py tests/unit_tests/symphony/test_experience_flow.py tests/unit_tests/symphony/test_toolkit.py -o addopts='' -o log_cli=false -q
```

其中 Tool 新测试 18 项、启动 7 项、既有沉淀/Toolkit 94 项。证据链采用真实 Core Flow 和真实本地 SkillManager；评审器是固定批准桩，Host push 使用 mock。覆盖同用户跨会话、其他用户拒绝、内容漂移、禁用/非普通成员、确认前不安装、确认后安装及幂等回执、旧 Core 检测、四种注册开关组合，以及旧 API 即使持有历史成功回执也不能绕过作用域。

Core 配套的三个 Flow 测试文件最终组合 **86 项通过**，详见 agent-core 的 `F_20261026_2032_skillpack-members`，不将本地源码验证视为旧 pin 已具备新能力。

直达相关定向组合 **46 项通过**，包含真实 DeepAgent 显式 Pack 执行以及 checkpoint 丢失内存 token 后的拒绝恢复。SkillNet 背景安装的库存提交测试 **29 项通过**（新增 4、既有 25），覆盖下载不占锁、提交统一占锁及共享成员覆盖保护。

最后一次“评审等待期间成员变化”修复后的 Swarm 组合 **730 项通过，43.39 秒**，涵盖 Symphony 全目录、Marketplace 全目录、直达、SkillPack backend/安装、Hub 安装/卸载、原编排问答/超时及 SkillNet。完整命令与日志由项目交付报告及 `work/backend-final.log` 保存。

最终评审时间窗修复后，在相同源码与 Python 环境执行以下组合，**87 项通过，10.07 秒**（Tool 20、既有 experience 67）：

```bash
PYTHONPATH=/Users/wujianyu/Documents/layshang/Projects/skillpack-productization/work/jiuwenswarm:/Users/wujianyu/Documents/layshang/Projects/skillpack-productization/work/agent-core /Users/wujianyu/Documents/layshang/Projects/symphony-integration/work/.venv/bin/python -m pytest tests/unit_tests/symphony/test_skillpack_tool.py tests/unit_tests/symphony/test_experience_flow.py -o addopts='' -o log_cli=false -q
```

新用例分别在异步评审期间修改成员内容、禁用成员，确认两者都在调用恢复/安装之前返回 `members_changed_reconfirm`。真实 SkillManager 确认安装用例继续通过，验证嵌套使用同一库存锁不会阻塞正常路径。测试没有证明绕过产品接口的外部进程文件修改可被库存锁约束。

## 真实环境修复与验证

2026-10-27 真实模型验收发现 `skillpack_distill` 在 supervisor 的工具任务中返回 `trusted_session_required`。原工具只依赖入口 Task 的 ContextVar，而既有 `SessionMessagingRouteRail` 只为会话消息工具恢复 run context。现将沉淀 Tool 纳入同一可信绑定与清理范围，不从参数读取会话 ID。新增并发回归验证两个会话互不串用、调用结束清理；沉淀 Tool 与会话消息回归共 171 项通过（`asyncio_mode=auto`）。

同次验收发现 Qwen 的边评估响应偶尔用单个 Markdown JSON 代码块包装，导致严格解析丢弃实际成功的边。宿主 `_EdgeEvaluationModelAdapter` 只移除完整且唯一的代码块边界；不提取散落 JSON、不使用 JSON repair，也不修改 Core 对重复字段、额外字段、状态和长度的严格检查。修复后沉淀 Tool 与既有经验链路回归共 94 项通过。此前被拒绝的证据没有人为改成成功，真实流程重新执行验收。

2026-10-27，使用实际 app/web、配套 wheel 和 Qwen3.8-Flash 验证：两个普通成员样例执行形成 3 条合格 trace，经实际评审和用户界面确认安装 Pack；“我的技能”、详情成员列表与 Picker 均展示安装结果。新 query 自动匹配并询问，确认后按根和声明成员执行，生成结构化 JSON 与 Markdown 报告。实际会话工具记录的 `symphony_compose_graph`、`symphony_refresh_graph` 调用均为 0。验证使用配套 wheel，不等同于最终 pin/lock 安装。

同日后续完成真实隔离 SkillHub 的发布、人工审核，以及同机独立新用户工作区从技能广场下载安装并确认直达执行。Hub 的资产、审核、存储和下载使用真实实现，登录由本地测试身份服务提供；生产 OAuth、自动安全扫描和跨机器执行尚未验收。该演示早于本次作用域修复，本轮只完成行为回归，未替换 5273 环境的既有 wheel。

## 已知遗留

| 遗留 | 性质与影响 | 后续验证入口 |
| --- | --- | --- |
| 新 Core 与 Swarm 正式依赖 | 当前分支 pin/lock 已切换配套 Core fork 的固定提交；官方合入尚未完成 | Core PR 合入后切回官方提交；真实模型 E2E 另验 |
| 模型评审与自动完整覆盖判断 | 真实站会文件样例通过，尚无大规模质量结论 | 固定任务集验证误匹配、漏召回和否定条件 |
| 生产身份、安全扫描与跨机器执行 | 隔离 Hub 人工审核及同机独立用户闭环已通过；未向正式市场公开资产，未验证生产 OAuth、自动扫描和跨机器样例执行 | 使用生产身份及可移植成员样例做专门验收 |
| 作用域隔离的模型质量 | 确定性隔离回归使用真实 Core Flow 与固定评审器；未据此宣称新实现已完成真实模型验收 | 独立用户数据隔离环境验证候选叙事和评审质量 |

## 2026-10-28：跨作用域候选消失修复

提交前检视确认，Core 按结构聚合后的可变 recipe provenance 会纳入其他会话的新记录；宿主再要求全部记录属于当前作用域，导致 A 会话已有 3 条合格记录后，B 会话加入 1 条同结构记录即让 A、B 都看不到候选。恢复历史快照仅用于先前演示，不能代替产品修复。

改用 `ScopedCandidateStore` 只读证据视图，在聚合前筛选真实归属、可选 `source_refs` 和当前成员快照。每个固定视图使用 Core 公开 `FlowStore`/`SymphonyFlowEngine`，在独立派生目录重新计算阈值、叙事、评审和产物，保持原证据与全局历史不变。选择此方案而非只复用历史版本，因为 A/B 交错执行时可能从未形成完全属于 A 的不可变合格版本。

缓存最多 8 个 worker，并用候选操作锁防止使用中淘汰；关闭时排空 worker。确认身份包含过滤范围，安装从服务端记录恢复该范围并重验当前身份、内容与启用状态，不接受模型提供 owner。生成确认与安装在异步评审结束后都重验身份；安装保留原有库存锁并再次检查成员与原始证据。只保留 recipe 而原始证据已丢失时拒绝继续，不把派生产物当作执行事实。真实演示历史未修改。

2026-10-28，修复最终行为组合 **140 项通过，40.98 秒**（Tool 39、启动 7、既有 experience 67、Toolkit 27）。覆盖 A 会话 3 条合格记录后 B 加入 1 条同结构记录、A/B 交错记录、同用户跨会话、`source_refs` 独立阈值与确认标识、确认期间身份变化、原始证据丢失、成员内容/启停变化、服务重启恢复与回执幂等、worker 有界缓存及关闭。测试使用真实 Core Flow 和本地 SkillManager，评审器固定批准，不代表真实模型质量复验。

第一次组合测试因本机缺少默认 `config.yaml` 在收集阶段失败；后续使用独立临时空配置通过，不改真实配置或执行历史。复现命令如下（从 Swarm 仓库根目录运行）：

```bash
/Users/wujianyu/Documents/layshang/Projects/symphony-integration/work/.venv/bin/python - <<'PY'
from pathlib import Path
import os, subprocess, tempfile
with tempfile.TemporaryDirectory(prefix='skillpack-scope-tests-') as directory:
    config = Path(directory) / 'config'
    config.mkdir()
    (config / 'config.yaml').write_text('{}\n')
    env = dict(os.environ, JIUWENSWARM_DATA_DIR=directory, PYTHONPATH='.:../agent-core')
    result = subprocess.run([
        '/Users/wujianyu/Documents/layshang/Projects/symphony-integration/work/.venv/bin/python', '-m', 'pytest',
        'tests/unit_tests/symphony/test_skillpack_tool.py',
        'tests/unit_tests/symphony/test_startup_singleflight.py',
        'tests/unit_tests/symphony/test_experience_flow.py',
        'tests/unit_tests/symphony/test_toolkit.py',
        '--no-cov', '-q', '-o', 'log_cli=false',
    ], env=env)
    raise SystemExit(result.returncode)
PY
```

最终组合检查发现导入边界测试未登记本次证据视图使用的两个公开接口。核对 Core `openjiuwen.symphony.flow.__all__` 后，仅允许从该 facade 导入 `FlowStore`、`RecipeEvidence`；没有新增任何深层模块白名单。补充回归继续拒绝 `flow.store`、`flow.engine`、深导入别名、未知符号与 `import *`。下列 3 项边界行为测试通过（41.72 秒），未运行格式化或静态检查工具：

```bash
PYTHONPATH=.:../agent-core /Users/wujianyu/Documents/layshang/Projects/symphony-integration/work/.venv/bin/python -m pytest tests/unit_tests/symphony/test_direct_service.py::test_production_uses_only_stable_openjiuwen_symphony_imports tests/unit_tests/symphony/test_direct_service.py::test_static_import_guard_detects_openjiuwen_symphony_alias_bypass tests/unit_tests/symphony/test_direct_service.py::test_flow_facade_import_guard_keeps_deep_imports_forbidden --no-cov -q -o log_cli=false
```

## 提交前配套依赖验证

2026-10-28，Core PR：<https://gitcode.com/openJiuwen/agent-core/merge_requests/3457>。Swarm 的三处依赖声明和 `uv.lock` 同步锁定 fork 提交 `8b69089e477303882301a4479fcf5758deb8ab59`；正式合入前需切回官方仓库的对应提交。本次重新解析也补齐基线 pyproject 已声明但旧 lock 缺失的 JiuwenMemory/jieba 和协议包记录，没有升级无关依赖版本。

新建独立环境，从 GitCode 实际构建安装该 Core 提交，并核对 `direct_url.json` 与实际 import 路径；不使用本地 Core checkout 覆盖。复用既有环境的第三方测试依赖，最终 Symphony、marketplace、直达、安装/卸载及相关行为组合 **750 项通过，129.59 秒**。前端安装 7、发布界面 17、文案 4 项通过；Node 24.19.0 的 Vite 构建通过（分块体积和动态导入提示）。

完整 `uv sync --frozen` 未通过：本机为 Intel Mac，基线已锁定的 `onnxruntime==1.25.1` 没有该平台 wheel；未为此扩大范围修改依赖。本次固定 Core 安装及回归不能替代完整冻结安装验收。此前演示环境继续保留原配套 wheel，本轮作用域/旧数据修复尚未重跑真实模型 E2E。

同日同步两仓 develop 后，Swarm 候选为 `b8e02958458809824d4e34ccc274684ac610567f`，实际安装和锁定的 Core 为 `2c10d96136cf83a3f3c96fa8286329e89d15fe09`。沿用上述独立环境，重新核对安装来源并运行同一后端组合：**750 项通过，130.78 秒**；Core 组合 138 项通过，前端 28 项及 Vite 构建重新通过。`uv lock --refresh-package openjiuwen` 成功；相对新 develop，依赖差异仅为配套 Core 的固定来源，不再包含无关 lock 修补。正式官方 pin、完整冻结安装和新修复的真实模型复验仍按上述边界处理。
