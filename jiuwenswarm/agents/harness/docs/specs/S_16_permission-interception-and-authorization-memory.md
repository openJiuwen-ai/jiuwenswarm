# S_16 权限拦截与授权记忆

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_16 / permission-interception-and-authorization-memory |
| 关联模块（agent-core 仓） | `openjiuwen/harness/rails/security/tool_security_rail.py`（`PermissionInterruptRail` 858 行 / 模块级 `compute_auto_confirm_key` / `compute_batch_allow_key`）、`openjiuwen/harness/rails/interrupt/confirm_rail.py`（基类与 ConfirmPayload）、`openjiuwen/harness/security/host.py`（`ToolPermissionHost` 回调契约）、`openjiuwen/harness/security/core.py`（`PermissionEngine` 判定）、`openjiuwen/harness/security/models.py`（`PermissionConfirmResponse` / `PermissionLevel` / `PermissionResult`）、`openjiuwen/harness/security/patterns.py`（merge / 写盘）、`openjiuwen/harness/security/shell_ast.py`（`parse_shell_for_permission`） |
| 关联模块（jiuwenswarm 仓） | 无本体文件——本 spec 管 agent-core 侧权限 rail 本体；宿主对 `ToolPermissionHost` 回调的实现（`_persist_allow_rule` / `_request_permission_confirmation` / `_permission_scene_hook` / `_get_permissions_snapshot`）与三级 rail 选择归 [[S_17]] |
| 最近一次修订日期 | 2026-10-26 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **工具权限拦截的判定序与授权记忆的分级存储**——`PermissionInterruptRail`（继承 `ConfirmInterruptRail`，`priority=90`，`inherit_to_subagents=True`）：

- **拦截范围**：对**任意**工具名执行 `before_tool_call` 判定（`tool_names` 仅作 `get_tools` 展示标签，不参与是否拦截）
- **判定序**：scene_hook 短路 → engine 判定（ALLOW/DENY）→ session auto_confirm → 批次 allow → hosted confirm → interrupt 弹卡
- **双 key 体系**：session 级 auto_confirm key（用户显式选择的记忆粒度）与 replay 级 batch allow key（参数指纹）
- **授权记忆三级**：allow_once（无记忆）/ allow_always_session（session state）/ allow_always_persist（磁盘 yaml）
- **persist 回滚**：磁盘写失败回滚内存配置；先读磁盘快照防复活用户已删条目
- **host 回调契约**：`ToolPermissionHost` 六个回调的返回值语义

不管：

- 三态决策协议与基类生命周期（ConfirmPayload 字段定义、`_skip_tool` 契约、AbortError cause 链）→ [[S_15]]
- 宿主侧回调实现与装配（channel 分流、hosted 卡片、scene hook 的 ask_user 豁免、三级 rail 选择）→ [[S_17]]
- `PermissionEngine` 内部规则匹配算法（ALLOW<ASK<DENY 序、owner_scopes 收紧）——契约见 [[S_13]] N11 同源表述；本 spec 只管调用侧
- 中断相位 / 权限响应台账 / 续跑注入 → [[S_18]]
- 取消结算与残留卡关闭 → [[S_19]]
- 子代理侧权限裁决（手工合成引擎 + Future 委托）→ [[S_13]]
- SkillTurbo / SkillAuthorization 的 rail 变体 → skill_turbo 域 [[S_06]] / [[S_09]]

## 不变量

- **N1（拦截一切工具）**：`before_tool_call` **不按 `tool_names` 过滤**——任意工具名都进入判定序；`tool_names` 仅用于日志与 `get_tools` 展示。放宽为按名单拦截会造成未注册工具绕过权限。
- **N2（嵌套 HITL 通道约束）**：`inherit_to_subagents=True`（factory 按**引用**注入父 rail 到子代理）。产品宿主（如 jiuwenswarm）在父会话卡上呈现子代理工具的 ASK 时，`request_permission_confirmation` **必须返回 `PermissionConfirmResponse` 而非 `"interrupt"`**——嵌套 checkpoint HITL 会打断外层 task_tool。auto_confirm 仍落在持有 ctx 的子会话上。
- **N3（判定序固定）**：`resolve_interrupt` 首查（user_input 为 None）顺序不可调换：
  1. `permission_scene_hook` 短路（approve / reject 两态，见 [[S_17]] 的豁免清单；hook 异常仅 warning，回落后续序）
  2. `_refresh_permissions_for_tool_call` → `engine.check_permission`：ALLOW → approve；DENY → reject（`[PERMISSION_DENIED] {reason}`）
  3. session auto_confirm 命中 → approve
  4. `RESUME_BATCH_ALLOW_KEYS` 命中 → approve（仅 replay 期，[[S_15]] N2）
  5. hosted `request_permission_confirmation`：返回非 `"interrupt"` 即就地结算
  6. 否则 `interrupt(ConfirmPayload schema)` 弹卡
- **N4（engine 异常 fail-closed）**：`check_permission` 抛异常直接 **raise**（不吞掉不静默放行）；`_refresh` 失败回落 `_static_config`。快照刷新的目的：与磁盘 permissions 对齐——若只写盘不刷新内存，旧 `_static_config` 会抹掉 `approval_overrides` 等运行期改动。
- **N5（session auto_confirm key 语义）**：`compute_auto_confirm_key`（**模块级**，rail 与 handler 批次计算共享，防两侧漂移）——裸工具名；bash / mcp_exec_command / create_terminal 用 shell AST：simple + 单子命令 + 无 risky 结构 → `tool:subcommand`，否则 `""`（**不可记忆**，弹卡不给「本会话内允许」提示）。用户在什么粒度记忆是用户显式选择，不由系统扩大。
- **N6（batch allow key 与 auto_confirm key 语义分离）**：`compute_batch_allow_key = {tool_name}:{sha256(canonical_args)[:12]}`——**全参数指纹**（sort_keys + ensure_ascii=False + default=str 的 canonical JSON）。同工具**同参数**才共享 key：单个 allow_once 不得扩大到用户未审阅过参数的兄弟调用（防静默授权扩张）。任何解析失败返回 `""`（fail-closed）。
- **N7（persist 三真与磁盘防复活）**：`persist_allow` 路径需 **approved ∧ auto_confirm ∧ persist_allow** 三真（`auto_confirm=True + persist_allow=False` = 仅 session 记忆）。`_persist_allow_always` **先经 host 快照读磁盘基线**（用户已从 config.yaml 删除的条目不得被内存 merge 复活；快照不可用时才 deepcopy engine.config）→ merge 工具级 allow 规则 + file_guard 路径白名单（按 `_collect_file_guard_persist_accesses` 收集本次 ASK 的 `(path, action)` 轴）→ `update_config` → 写盘。
- **N8（写盘失败回滚）**：host `persist_allow_rule` 返回假或抛异常，或直接 `write_permissions_section_to_agent_config_yaml` 失败 → `update_config(prev_cfg)` 回滚内存配置（内存与磁盘一致，防止「内存放行但重启后复现弹卡」的假允许）。
- **N9（session 记忆五条件）**：`_should_store_auto_confirm = approved ∧ auto_confirm ∧ session 存在 ∧ auto_confirm_key 非空 ∧ ¬persisted`——已持久化到磁盘的规则由 PermissionEngine 在会话启动时加载，session 层不重复存。
- **N10（hosted confirm 返回三分类）**：`request_permission_confirmation` 返回——`"interrupt"` → 走 interrupt 弹卡；`None` → reject（`[PERMISSION_DENIED] ... (Hosted permission request failed)`，宿主请求失败按拒绝结算）；非 `PermissionConfirmResponse` 类型 → reject（`(Invalid hosted permission response)`，warning 记录）；合法对象 → 就地结算（approved → approve；否则 reject feedback 或 `[PERMISSION_REJECTED]`）。
- **N11（用户响应解析失败再问）**：`parse_confirm_payload`（`PermissionConfirmResponse` 直通 / `ConfirmPayload` 转换 / dict 校验 / str-JSON 递归）解析失败 → **再 interrupt**（fail-closed，不静默放行也不静默拒绝）。
- **N12（工具名归一化）**：`TOOL_NAME_ALIASES`（free_search→mcp_free_search / paid_search→mcp_paid_search / fetch_webpage→mcp_fetch_webpage / exec_command→mcp_exec_command）在判定与持久化前统一归一——配置用 mcp_* 名，harness tools 用裸名。
- **N13（trusted_dirs 请求级热更新）**：`set_trusted_dirs` 把宿主随请求下发的目录归一为绝对 Path 后 `engine.update_trusted_dirs`——external_directory 检查把这些子树视为内部（跳过 ask/deny）；persist 时的外路径检测复用同一 trusted_dirs（内存判定与落盘判定一致）。
- **N14（弹卡消息有界）**：`format_args_preview` JSON 序列化截断 1000 字符；`_build_always_allow_hint` 仅当 auto_confirm_key / shell_key 非空才生成提示（`""` key 工具不展示记忆选项）。

## 接口契约

### `PermissionInterruptRail`（priority=90，inherit_to_subagents=True）

| 成员 | 契约 |
|---|---|
| `__init__(config, engine, tool_names, llm, model_name, host)` | engine 未注入则按 `_static_config` + host.resolve_workspace_dir 构造（解析失败仅 debug）；host 注入 `tool_permission_checks_active` |
| `before_tool_call(ctx)` | N1 不过滤工具名；取 user_input → 读 session auto_confirm → `resolve_interrupt` → 决策存 `ctx.extra["_interrupt_decision"]` → `_apply_decision`（三态契约见 [[S_15]] N4） |
| `resolve_interrupt(ctx, tool_call, user_input, auto_confirm_config)` | N3 判定序；用户响应分支：解析（N11）→ persist 三真（N7/N8）→ session 五条件（N9）→ approve / reject |
| `parse_confirm_payload(user_input)` | N11 四分支，返回 `PermissionConfirmResponse \| None` |
| `parse_tool_args(tool_call)` | str-JSON / dict / 其它 → dict（解析失败 `{}`） |
| `set_trusted_dirs(trusted_dirs)` | N13 请求级热更新；None/空清空 |
| `update_config(config, tool_names=None)` | 热更新 `_static_config` + engine；可选更新展示标签集合 |

### 模块级函数

| 函数 | 契约 |
|---|---|
| `compute_auto_confirm_key(tool_call)` | N5；模块级共享（注释明示：resume 批次计算与 rail 匹配不得漂移） |
| `compute_batch_allow_key(tool_call)` | N6；fail-closed |

### shell key（`build_shell_auto_confirm_key`）

| 输入条件 | 输出 |
|---|---|
| 空 / 非 simple / has_risky_structure / 子命令数≠1 | `""`（不可记忆） |
| simple 单子命令 | `tool_name:subcommand` |

### `ToolPermissionHost` 回调契约（agent-core 定义，宿主实现见 [[S_17]]）

| 回调 | 契约 |
|---|---|
| `permission_scene_hook` | 场景豁免短路（approve / reject(msg)）；异常回落主序 |
| `request_permission_confirmation` | N10 三分类；返回 `"interrupt"` 表示走弹卡 |
| `get_permissions_snapshot` | 请求级配置快照（overlay 组合后的 dict）；None 用 `_static_config` |
| `persist_allow_rule` | 磁盘写回调；返回假 / 抛异常触发 N8 回滚 |
| `resolve_workspace_dir` | 工作区根（engine 构造与 file_guard persist 用） |
| `tool_permission_checks_active` | 引擎检查开关注入 |
| `permission_yaml_path` | 无 host 写回调时的默认 yaml 路径 |

## 数据结构

- `PermissionConfirmResponse`：approved / feedback / auto_confirm / persist_allow（与 `ConfirmPayload` 四字段一一对应）
- session auto_confirm 存储：`{auto_confirm_key: True}`（[[S_15]] N10 的写入侧在本 rail）
- batch allow 集合：`ctx.extra[RESUME_BATCH_ALLOW_KEYS]`，元素为 `tool_name:args_digest` 指纹
- 决策日志三元组：`permission.user.decision tool=... confirm_path=hosted|interrupt decision=allow_once|allow_always_session|allow_always_persist|deny persisted=...`

## 执行路径

1. **P1 首查**：任意工具调用 → N1 进入判定 → N3 序：scene_hook → engine（ALLOW 直通 / DENY 拒绝）→ auto_confirm → batch allow → hosted → interrupt 弹卡（消息含参数预览 + 匹配规则 + 路径 + 记忆提示）
2. **P2 用户响应（interrupt 回复）**：InteractiveInput 项解析 ConfirmPayload → N11 失败再问 → 三真 persist（P3）→ N9 session 存储 → approve / reject
3. **P3 持久化**：三真 → 磁盘快照基线 → merge 工具规则 + file_guard 轴 → update_config → host 写盘 / 默认 yaml → 失败回滚（N8）
4. **P4 session 记忆**：approved + auto_confirm + 非 persisted → `_store_auto_confirm` 写 `INTERRUPT_AUTO_CONFIRM_KEY`
5. **P5 replay 批次**：resume 重放中兄弟调用命中 batch key → approve（key 生命周期见 [[S_15]] N2）

## 异常分支与退出路径

- `check_permission` 异常：**raise**（fail-closed，调用方收到异常而非静默放行）
- scene_hook 异常：warning，回落主序
- hosted 返回 None / 非法类型：reject（N10）
- persist 写盘失败：内存回滚（N8），本次按未持久化处理（session 记忆仍可存）
- 用户响应解析失败：再 interrupt（N11）
- workspace 解析失败：engine 以 workspace_root=None 构造，external_directory 判定退化

## 边界 case

| 场景 | 行为 | 依据 |
|---|---|---|
| 工具未注册到 tool_names | 仍被拦截判定 | N1 |
| 子代理工具 ASK（产品宿主） | hosted 返回 PermissionConfirmResponse 就地结算，不嵌套 checkpoint | N2 |
| 复合 shell 命令（`cd x && rm y`） | auto_confirm key 为 ""，不可会话记忆 | N5 |
| 同工具不同参数的兄弟调用 | 不共享批次授权 | N6 |
| 用户已从 yaml 删除条目后再次 persist | 磁盘快照基线，不复活已删条目 | N7 |
| persist 写盘失败 | 内存回滚，不产生假允许 | N8 |
| hosted 请求失败（None） | reject PERMISSION_DENIED | N10 |
| auto_confirm + persist_allow 双勾 | 磁盘记忆，session 不重复存 | N9 |
| trusted_dirs 随请求到达 | external_directory 跳过这些子树 | N13 |
| 弹卡参数超长 | 截断 1000 字符 | N14 |

## 修改检查单

改 tool_security_rail.py / host.py 前：

- [ ] before_tool_call 仍未按 tool_names 过滤（N1）？
- [ ] 判定序六步未调换（尤其 auto_confirm 在 engine 之后、batch 在 hosted 之前）（N3）？
- [ ] engine 异常仍 raise 不吞（N4）？
- [ ] `compute_auto_confirm_key` 仍模块级共享（rail 与 handler 不各自实现）（N5）？
- [ ] batch key 仍全参数指纹、fail-closed（N6）？
- [ ] persist 仍先读磁盘快照（N7）？
- [ ] 写盘失败回滚未被移除（N8）？
- [ ] session 五条件未放宽（N9）？
- [ ] hosted 三分类（"interrupt"/None/非法类型）处理完整（N10）？
- [ ] TOOL_NAME_ALIASES 新增别名时配置侧同步（N12）？
- [ ] persist 与运行期 trusted_dirs 同源（N13）？

## spec 关系

- 上游：[[S_15]]（ConfirmInterruptRail 基类、三态决策、ConfirmPayload 字段、五状态键——本 rail 覆写 `_get_auto_confirm_key` 与判定序）
- 下游：[[S_17]]（宿主实现 ToolPermissionHost 全部回调并三级选择 rail 变体）、[[S_18]]（权限响应台账包住 hosted confirm 周期）
- 跨域：[[S_13]]（子代理投射版 rail 与 PermissionEngine 同源；owner_scopes 只收紧）；skill_turbo [[S_06]] / [[S_09]]（SkillTurboPermissionRail / SkillAuthorizationPermissionRail 变体在装配侧逐级替换）；harness [[S_08]]（`[ERROR]:` 前缀与本域 `[PERMISSION_DENIED]` / `[PERMISSION_REJECTED]` 前缀互不消费）
