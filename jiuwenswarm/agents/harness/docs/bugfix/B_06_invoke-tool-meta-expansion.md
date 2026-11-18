# B_06 — invoke_tool 转发绕过权限命令级判定

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | bugfix |
| 编号 / slug | B_06 / invoke-tool-meta-expansion |
| 修复日期 | 2026-11-17 |
| 关联模块 | `interrupt/interrupt_helpers.py`（build_permission_rail）、`permissions/permission_runtime.py` |
| 关联 spec | [S_16](../specs/S_16_permission-interception-and-authorization-memory.md)、[S_17](../specs/S_17_host-assembly-and-interaction-conversion.md) |
| 关联 issue | GitCode [jiuwenswarm#6499](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6499)（↔ GitHub #11369），dev-stable / 【dev-stable专属】安全，版本 1.8.0.0 |
| 源码基线 | 本地 `dev-stable` `d572e5803`；配套 agent-security（agent_shield）develop |

## 现象与原因

企业架构下经 `invoke_tool` 转发 PowerShell 命令可绕过安全组件拦截。复现载荷
（issue 原样四条）：

1. `[ScriptBlock]::Create("Get-Date")`
2. `& (Get-Command notepad)`
3. `powershell.exe -EncodedCommand aABlAGwAbABvAA==`
4. `iex "Get-Process"`

绕过链（三层叠加）：

- 外层判定失明：`invoke_tool` 不在 `resources/config.yaml` 的 `tools` 列表，
  TieredPolicy 走 `defaults: {'*': allow}` 直接放行——权限 rail（拦截一切工具）
  尽责执行了，但评估的是"信封"，嵌套 `arguments.command` 里的真实命令无人识别。
- 内层执行裸奔：`ProgressiveToolRail._invoke_target_tool_with_binding` 直接
  `target_tool.invoke(arguments)`，不经 before_tool_call 链（该文件 1767–1769 行
  注释自证此点，且仅视觉工具补了就地拦截）。
- 安全组件无转发概念：agent_shield 的组件与规则按平铺 `(tool_name, args)`
  设计，未注册任何"工具执行工具"的展开契约。

直接调用 `bash` / `create_terminal` 时四条载荷均被命令级判定（pwsh AST、
fileguard、tiered 规则）覆盖——问题不在检测能力，在转发路径让检测看不到命令。

## 修复与取舍

采用**判定层展开**（改动落在 agent-security 引擎，本仓一处注册 + 接线）：

- agent_shield `tool_permission` 新增 meta-tool 注册表（`meta_tools.py`）：
  宿主注册转发契约（工具名 + 内层对提取器）；`PermissionEngine.check_permission`
  对注册转发器先评外层、再对内层 `(T, A)` 递归判定（深度封顶 2；**封顶处外层
  仍是注册转发器时并入不可静默放行的 ASK fail-closed**，防深层嵌套经外层
  defaults 重新打开绕过），与外层合并取最严，同级平手保留内层 matched_rule
  （审批卡展示真实命令命中的规则）。
- 代码质量检视（2026-11-17）修正两处：引擎对 extractor 输出的重复校验删除
  （单点收敛在注册表契约）；英文开发指南补齐对应段落。
- 本仓 `permission_runtime.install_meta_tool_expanders()` 注册 invoke_tool 的
  提取契约（幂等）；`build_permission_rail` 在权限 enabled 门通过后调用
  （S_17 装配点）。权限系统关闭时降级语义不变（不注册、不展开）。
- review 修正两处（2026-11-17，GitCode !10448 检视意见）：
  - **注册动作纳入 fail-open 装配契约**：`install_meta_tool_expanders` 的
    import 与调用一并包进调用点 try/except Exception——注册失败（或
    agent_shield 依赖漂移致符号 ImportError，即 CI 旧 pin 轮的崩溃形态）
    告警 + 降级为仅外层判定，权限 rail 照常构建，不再以未声明方式冒泡出
    `build_permission_rail`（S_17 铁律 34，与 docstring「构造失败
    warning + None」同一语义）。
  - **提取器判据与返回值统一**：非空判据按 `strip()`，返回值也归一为
    strip 后名字——引擎 tiered 按精确键匹配 tools 配置
    （`tiered._tools.get(tool_name)`），空白填充的内层名原样透传必然失配、
    落回 defaults 放行，判据与返回值不一致等于漏开一条空白旁路。

刻意不做（各有理由，非遗漏）：

- **invoke 站点终检**：外层判定与内层执行之间参数不漂移（office_claw 恢复只换
  实例不改名字/参数），rail 挂载时引擎展开已全覆盖；"rail 整体未挂载"是 S_17
  fail-open 降级语义，加后哨会改变降级部署行为，需独立设计决策。
- **SSAS 观测解包**：`tool_call_to_event` 适配器无生产消费方；SSAS 检测模块的
  嵌套感知另立 issue。
- **shell 工具去延迟化（pin 常驻）**：上下文预算的产品取舍，独立评审。

已知缺陷（质量检视发现，后续独立修复，不阻塞本修复）：

- **转发卡上的“总是允许”记忆失真**（2026-11-18 按 !10448 检视意见重述，并按
  交付锁 openjiuwen `c30cbec5` 逐环节核验；早先“`merge.skip` 复评门拦截落盘”
  的描述过时——该复评门已由 agent-core `d6d66a883` 移除，验证锁 `c4d7ec67`
  与交付锁均无此机制，两锁的权限路径零差异）：
  - persist 侧无 ASK 复评门（`approval_rules.merge_permission_allow_rule_into_permissions`），非 shell 工具直接落盘**外层整工具规则** `tools.invoke_tool: allow`——粒度过宽；
  - 引擎对已注册转发器无条件展开、合并取最严（agent_shield `engine.py`
    `_evaluate_expanded`），该外层规则压不住信封形态调用的内层判定——每个
    新会话首次同类转发仍弹卡，“总是允许”的跨会话记忆对转发卡等于失效；
  - 首次点击因已落盘不存 session auto_confirm（`_should_store_auto_confirm`
    的 `not persisted` 门）→ 同会话同命令再弹一次；第二次点击 merge 无可
    变更项、返回未落盘，转存裸键 `invoke_tool` 的 session auto_confirm
    （`compute_auto_confirm_key` 对非 shell 工具即裸工具名，对本会话全部
    内层工具放行，亦过宽）后静默。
  方向保守（整体严格于修复前“外层 defaults 全静默放行”的基线，无新增静默
  放行面），非安全洞；修复需 agent-core persist 侧支持内层规则落盘 / 会话键
  细化（S_16 管辖），另立 issue。
- **内层对提取契约在本仓存在两份实现**：本修复的注册提取器（args Mapping）
  与 `team_invoke_aware_permission_rail.synthetic_target_tool_call_from_invoke`
  （ToolCall/JSON，且拒绝自转发）验证规则已分叉；统一需重构 team rail，后续
  issue 一并处理。

## 验证与限制

- agent-security 侧（本修复判定主体）：新用例 16 通过（先红后绿：引擎接入前
  四条载荷经注册转发器仍 ALLOW，即 issue 复现；接入后 ASK/DENY）；既有
  `tests/tool_permission/` 全量 **1519 通过、3 跳过**，零回归——未注册转发器的
  进程行为逐位不变（defaults/外层显式 deny/extractor 弃权/深度封顶均有用例钉住）。
- 联合验证（本仓侧，2026-11-17）：开发 venv 含手装 editable 的超前 agent-core
  （未推的 820e6f6e4 重构引用 `ShellStructureFlags`，agent-security 所有可获取
  ref 均无此符号，既有权限测试于收集期 ImportError，属跨仓 WIP 不同步的既有
  环境问题），故按锁另建 scratch venv 复现 CI 态——`UV_PROJECT_ENVIRONMENT=
  .venv-verify uv sync`（验证时锁态 openjiuwen=`c4d7ec67`，交付锁已随
  dev-stable 引用前移至 `c30cbec5`——前者为后者祖先，两锁在
  `harness/security/` 与 `harness/rails/security/` 下零差异，结论不受锁前移
  影响；两者均早于该重构，无错位）+
  本地 agent-security agent_shield editable 覆盖。结果：新用例
  `test_permissions_meta_tool_expansion.py` **12 通过**（四条载荷 / 良性放行 /
  外层显式 deny / 幂等 / 装配接线 / 禁用不注册 / 内层名空白归一 /
  注册失败装配不崩溃×2——后三项先红后绿钉住 review 修正）；既有权限回归
  `test_interrupt_helpers.py` + `test_permissions_persist_file_guard.py` +
  `test_skill_authorization_permission_rail.py` **75 通过**。review 修正后同
  环境复验 CI 旧 pin 轮（!10448 首跑，head 66c899c8 仍钉 `fceecb12`）的
  197 失败 + 5 收集错误全数归因旧 pin ImportError 级联（含静默降级的
  grants==0 / 消息不匹配变体）：同批文件 275 + 250 通过，仅
  `test_file_skill_grant_store.py` 两条 Windows symlink 权限用例因本地
  无符号链接特权失败（CI 为 Linux，不受影响，非回归）。
- relay-claw（OfficeAce）无需参与：修复执行点在本仓 rail 链与 agent_shield
  引擎；relay-claw 全仓检索（ts/js/py，排除 node_modules/dist）`authorize_tool`
  仅 PPT 翻译扩展命中——`extensions/pptx_translation/native_agent.py` 的
  `BoundPageTool.invoke` → `authorize_tool` 对 Progressive 直调 `Tool.invoke`
  的页工具复用目标权限 rail（注释自证 "Progressive invoke_tool dispatches
  directly to Tool.invoke, outside ReAct's target-tool rail chain. Apply the
  same target permission here."），非本次暴露面。另注：relay-claw 主运行时在
  catalog 同步后主动把 `invoke_tool` 预置为 allow
  （`packages/api/src/domains/agents/services/agents/providers/relayclaw-shared-runtime.ts`
  `ensureDefaultToolPermissions`，否则每次调用落 defaults.guard → ASK 阻塞
  远程 MCP 连接器）——该外层放行在引擎展开 + 取最严之后不再携带内层穿透，
  恰为本修复覆盖面，同样无需 relay-claw 侧改动。issue 暴露面为本仓
  builtin_rules 钉的 bash / mcp_exec_command / create_terminal。
- 发布顺序约束：jiuwenswarm `pyproject.toml` 钉 agent-shield 具体 commit
  （须为含 `meta_tools.py` 展开能力的版本，即 agent-security !129 及其后；
  具体钉值以 pyproject.toml / uv.lock 为准，不在文档里复述——钉随合入形态
  前移，复述必漂移；注释要求与 core 的钉同步），**接线、测试与 pin bump
  必须原子合入**——先合 agent-security（!129 → develop），后合本仓 PR，
  否则 CI 安装的旧 agent_shield 无展开能力，用例于收集期失败（!10448 首跑
  UT/ST 全挂即此形态：197 失败 + 5 收集错误 + ST `chat.error ImportError`，
  pin 前移后复验全绿）。若 !129 以 squash 合入，所钉 commit 不进目标分支
  历史，本仓 pin 需按合并结果再前移一次。
