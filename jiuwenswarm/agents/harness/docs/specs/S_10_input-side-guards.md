# S_10 输入侧守卫（路径修复 / 读文件校验 / 工作区配额门禁）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_10 / input-side-guards |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/agents/harness/common/rails/path_whitespace_fix.py`（纯函数库）；`.../rails/read_file_validation.py`（纯函数库）；`.../rails/workspace_quota_rail.py`（`WorkspaceQuotaRail` + `check_workspace_command_tool`）。宿主：`.../rails/stream_event_rail.py`（`JiuSwarmStreamEventRail`：`before_tool_call` 内空白修复 / 读拦截 / 原生图片分支，`after_tool_call` 内归一；图片语义归 [[S_22]]）。装配：`agents/swarm/providers/builtin_rails.py`（`swarm.workspace_quota` :86-91）、`agents/swarm/config_specs.py`（:99 / :149 rail 名单）、`server/runtime/agent_adapter/interface.py`（:1411-1416 附件物化前配额检查） |
| 关联模块（agent-core 仓，跨仓契约） | `openjiuwen.core.single_agent.rail.base`（`AgentRail` / `AgentCallbackContext` / `ToolCallInputs`；`ctx.extra["_skip_tool"]` 跳过执行约定）、`openjiuwen.core.foundation.llm.ToolMessage`、`openjiuwen.core.sys_operation.cwd.get_cwd`（相对路径基准）、`openjiuwen.core.runner.callback`（钩子链按 priority **降序**执行——`framework.py:459` / `chain.py:67`） |
| 配额本体（common 域，跨域契约） | `jiuwenswarm/common/workspace/quota.py` —— 归 **common 域 S_07**（`jiuwenswarm/common/docs/specs/S_07_workspace-safety-and-quota.md`），本 spec 只登记薄挂载 |
| 最近一次修订日期 | 2026-10-31 |
| 关联 feature | F_01_native-multimodal-image-input.md |

## 范围 / 边界

本规约管**工具调用的参数与结果进入模型上下文之前**的三类输入侧守卫：

- **路径空白修复**：模型给出的读侧工具路径与真实路径仅空白字符不同（典型：中文文件名被插入空格）→ 自动改参 + 结果前置通知
- **读文件校验**：图片 / 二进制路径的执行前拦截（before）与二进制误读结果归一（after）
- **工作区配额门禁（命令工具侧薄挂载）**：配额 block 时拒绝 bash 等命令工具与企业附件下载

不管：

- 宿主 `JiuSwarmStreamEventRail` 的本体（sid 解析、事件发射、cwd rebind、call_goal 剥离、symphony、暂停 / 中止）→ 任务 7 事件流域
- 配额本体（策略选路、du 缓存、对账、写文件门禁 `check_workspace_write` / `check_path_write`）→ common 域 S_07
- LLM 调用层 / 工具循环熔断 / 并发注册安全 → [[S_07]] / [[S_08]] / [[S_09]]（harness 域）
- 权限审批拦截（permission interrupt / security rail）——另一条链

**范围排除甄别**：

1. "输入侧"指**工具调用边界**（before 改参 / before 拒绝 / after 归一），不是用户请求参数校验（server 域职责）。
2. 模型**流式输出**的空白保留（adapter 流式 chunk）与路径修复无关——曾有 `test_stream_chunk_whitespace.py` / `test_team_stream_whitespace.py` 实测后删除，勿恢复。
3. 编号注意：harness 域 S_07（LLM 调用层失败恢复）与 common 域 S_07（workspace-safety-and-quota）是**两个独立编号空间的同名编号**，跨域引用一律写全路径。

## 不变量

- **N1（只修读侧四工具）**：`WHITESPACE_FIX_TOOL_NAMES = {read_file, list_files, glob, grep}`。write / edit 故意排除——缺失路径可能是待创建文件；ACP `read_text_file` 排除——该工具由客户端 `fs/read_text_file` JSON-RPC 服务，路径在客户端文件系统，宿主 `os.path.exists` 判定不了。
- **N2（修复判定链 fail-closed）**：`resolve_whitespace_variant` 前置放弃条件——非 str / strip 后空 → None；`expanduser` 后 UNC 路径（`\\` 或 `//` 开头）→ None；相对路径无 `base_dir` → None；**原路径已存在 → None**；`parts < 2` 或首段非已存在目录 → None；`listdir` OSError → None；squash 后 key 为空 → None。整链宁可不修，绝不猜。
- **N3（恰一匹配才修）**：逐段下行——已存在段直接通过；缺失段以 `_squash`（去**全部**空白字符，`str.split` 覆盖全角空格）为 key 在 `os.listdir(current)` 中找 squash 相等的名字，`len(matches) != 1` → None。**歧义与零匹配同等放弃**；非空白差异（数字、扩展名不同）天然不命中。
- **N4（修参不可变 + 类型保持）**：`fix_whitespace_path_arguments` 返回**新** dict（浅拷贝 + 新 list），原入参不变异；JSON string 入参解析失败返 None，无 fixes 返 None。宿主 `_fix_whitespace_path_args` 写回 `tool_call.arguments` 时**保持到达时类型**：str → `json.dumps(ensure_ascii=False)`，dict → dict。
- **N5（通知明示不重试）**：`build_whitespace_fix_notice` 双语（cn / en），文案同时含原路径与纠正路径，并要求"后续直接使用该路径，不要重试原路径"。
- **N6（before 拦截注入）**：`handle_read_file_before_tool_call` 对非文本扩展名（图片 13 种 + 压缩 / 媒体 / 可执行 / 字体等 30 种）在执行前拒绝：`ctx.extra["_skip_tool"] = True` + `_write_read_file_tool_result` 写 `tool_result`（字符串）与 `tool_msg`（存在则改 content、缺 `tool_call_id` 则补齐；不存在则新建 `ToolMessage`）。原生多模态开启时改走 N13 分支，图片扩展名不再被这条路拒绝。
- **N7（二进制启发式双轨）**：`_looks_like_binary_payload`——特征词表（11 个 marker，lower 后包含匹配）**只对 ≤500 字符**的短文本生效（错误消息样）；任意长度看 `\x00` 与前 4096 字符控制字符（`ord < 9`、`== 11`、`== 12`、`14–31`；\t \n \r 除外）占比 `> 0.05`。**长文本不按词表判**——真实文件内容可合法含 "binary file" 字样（如读到的源码本身）。
- **N8（parallel_read 聚合放行）**：结果解包出 `{parallel_read: 真值, files: list}` → `validate_read_file_result` 直接 `(True, None)`——单文件失败已隔离在 `files[i].error`，聚合文本不再整体套启发式。
- **N9（after 归一幂等）**：`normalize_read_file_tool_outcome` 只对 read 类 7 工具名生效；结果已是 `[READ_FILE_ERROR]` 前缀（lstrip 后 startswith）→ return；`success is False`（dict 或 ToolOutput duck-type 解包）优先透传工具自带 error 文案；空结果 / 二进制误读改写为带前缀的引导文案（图片 → `visual_question_answering`）。**只改写结果，不置 `_skip_tool`**（after 阶段工具已执行完）。
- **N10（命令工具满额拒绝）**：`WorkspaceQuotaRail.before_tool_call` 只拦 `_COMMAND_TOOLS`（bash / exec_command / mcp_exec_command / execute_bash / run_command / shell / powershell）及 `名_` 前缀变体（strip + lower 归一）；quota 状态 `block` → 捕获 `WorkspaceQuotaExceeded` 后 `_deny`：`_skip_tool` + `tool_result` 为 **dict** `{success: False, error, code: WORKSPACE_QUOTA_EXCEEDED}`（attr / dict 两种 inputs 形态都写）+ `tool_msg` 为字符串。
- **N11（du / walk 卸线程池 + 附件同入口）**：`check_workspace_command_tool` 内含 `resolve_effective_quota`（近限同步 du，可能阻塞数秒），rail 侧经 `asyncio.to_thread` 卸载，避免卡 Agent 事件循环；`interface.py` 企业附件物化前**同步**调用同一入口（下载体积未知：已 block 直接拒绝，否则放行后由写文件门禁兜底）。
- **N12（开关 fail-open + 写侧不查）**：开关是 env `WORKSPACE_QUOTA_ENABLED`（真值 1/true/yes/on，默认**关**），非 config.yaml——关时 rail `before_tool_call` 直接 return、`check_workspace_command_tool` 返回 `allowed=True`；写文件工具不在此查（走 fs.write_file 的写侧门禁，common 域 S_07）。
- **N13（原生模式读图分支）**：`_native_read_image_enabled()` 为真时，`before_tool_call` 对 read 类路径列表（`extract_paths_from_arguments`，兼容单路径与 `file_paths` 批量）分三步：① 任一 path 命中本次请求已提供图片快照（`is_provided_image_path`，快照来源见 [[S_22]] N7）→ 整批拒绝（`build_provided_image_read_error`，避免重复 base64）；② 命中非图片的非文本扩展名 → 整批拒绝（`build_non_text_read_error`）；③ 其余（含未提供的图片）放行，由 `ReadFileTool` 按 resolver 注入像素。原生关闭时退回 N6 的单路径拦截 / 批量非文本拒绝。
- **N14（原生模式视觉工具拒绝）**：`_native_read_image_enabled()` 为真时，`before_tool_call` 对 `is_vision_tool`（`visual_question_answering` / `image_reading`）直接拒绝并提示图片像素已随消息提供、无需再调视觉工具（`build_native_image_tool_error`）；原生关闭时不拦截（VQA 是唯一读图通道）。

## 接口契约

### path_whitespace_fix（纯函数）

| 成员 | 契约 |
|---|---|
| `resolve_whitespace_variant(path, base_dir=None)` | N2/N3；返回唯一匹配的规范路径或 None |
| `fix_whitespace_path_arguments(arguments, base_dir=None)` | 单键 `file_path` / `path` + 列表键 `file_paths` 逐条修；返回 `(new_args, fixes)` 或 None；N4 |
| `build_whitespace_fix_notice(fixes, language="cn")` | N5；多修复按行拼接 |

### read_file_validation（纯函数 + ctx 辅助）

| 成员 | 契约 |
|---|---|
| `is_read_file_tool(name)` | 名单 7 名（read_file / read / Read / read_text_file / file_read / memory_get / read_memory），strip 后判 |
| `extract_path_from_arguments(arguments)` | dict / JSON string（`{` 开头才解析）按 `_PATH_KEYS`（path / file_path / file / target_file）取首个非空 str；裸字符串非 `{` 开头直接作路径 |
| `is_non_text_file_path(path)` / `is_image_file_path(path)` | 扩展名 lower 判定（30 + 13 种） |
| `build_non_text_read_error(path, language="cn")` | `[READ_FILE_ERROR]` 前缀 + 图片 / 二进制分型引导 |
| `validate_read_file_result(path, result)` | N7/N8 判定序：parallel_read 放行 → success=False 透传 error → 空结果 → 非文本扩展名且疑似二进制 → 疑似二进制；返回 `(ok, error_message)` |
| `handle_read_file_before_tool_call(ctx, path)` | N6 before 拦截 |
| `normalize_read_file_tool_outcome(ctx)` | N9 after 归一 |
| `reject_read_file(ctx, message)` | `_skip_tool` + 注入（仅 before 语义使用） |
| `resolve_language_from_context(ctx)` | inputs.language / preferred_language → extra 同名键 → 默认 cn |

### workspace_quota_rail

| 成员 | 契约 |
|---|---|
| `WorkspaceQuotaRail`（`AgentRail`） | `priority = 95`；`before_tool_call` 按 N10/N11/N12；`init` 仅 super（无自定义注册） |
| `check_workspace_command_tool()` | 独立可复用入口：关 → allowed；block → **raise** `WorkspaceQuotaExceeded(decision)`；否则 allowed + snapshot |
| `_deny(ctx, tool_name, tool_args)` | 静态方法；三处写（`_skip_tool` / tool_result dict / tool_msg str） |

### 装配

- **宿主（前两件）**：`JiuSwarmStreamEventRail`（priority 80）在 deep 主链（interface_deep :2622）、skill_turbo（executor :471）、swarm 成员（`swarm.stream_event`）、team 运行时继承（team_runtime_inheritance :202）四处挂载——守卫随宿主生效，无独立开关。
- **quota rail**：swarm `swarm.workspace_quota` 无参 builder；`_COMMON_RAIL_NAMES`（:99）与 `_CODE_RAIL_NAMES`（:149）均含。钩子链按 priority 降序：**quota(95) 先于 stream(80) 执行 before_tool_call**——quota 拒绝先置 `_skip_tool`，stream rail 据此跳过 in_progress 事件（tool_call 卡片仍发，after 仍发被拒结果）。
- **附件物化**：`interface.py` 企业附件需下载时调 `check_workspace_command_tool`（同步、无捕获——block 即请求失败，fail-closed）。

## 数据结构

- 名单常量：`WHITESPACE_FIX_TOOL_NAMES`（4）/ `READ_FILE_TOOL_NAMES`（7）/ `_IMAGE_EXTENSIONS`（13）/ `_NON_TEXT_EXTENSIONS`（30）/ `_BINARY_RESULT_MARKERS`（11）/ `_COMMAND_TOOLS`（7）/ `_PATH_KEYS`（4）/ `_SINGLE_PATH_KEYS`（2）/ `_MULTI_PATH_KEYS`（1）
- ctx.extra 键：`"_skip_tool"`（跳过执行，跨 rail 通用约定）、`"_jiuwenswarm_whitespace_path_fixes"`（`{tool_call_id: [(original, corrected), ...]}`——**整个 invoke 内所有工具调用共享一个 extra dict**，after 按 tool_call_id pop）
- quota 侧（common 域 S_07 摘要）：`QuotaSnapshot`（used / limit / percent / status `"ok"|"warn"|"block"` / soft 80 / hard 100 / unlimited）、`QuotaGateDecision`（allowed / status / detail / snapshot）、`WorkspaceQuotaExceeded`（`detail` 固定 `WORKSPACE_QUOTA_EXCEEDED`）
- 结果形态差异（有意为之）：read 校验注入 `tool_result` 为**字符串**；quota `_deny` 写 **dict**（结构化 `code` 供 UI / 上游识别）

## 执行路径全景

- **P1 路径修复（before）**：stream `before_tool_call` → `_rebind_runtime_cwd`（先执行，`get_cwd()` 已是新值）→ 工具名 ∈ 空白修复名单 → `_fix_whitespace_path_args`（base_dir 取 `get_cwd()`，取不到降级 None）→ 有 fixes → arguments 类型保持写回 + info 日志 → `extra[...][tool_call_id] = fixes`。
- **P2 读拦截（before）**：`is_read_file_tool` → `extract_paths_from_arguments` → 原生开启：N13（已提供图片拒绝 / 非图片二进制拒绝 / 图片放行）+ N14 视觉工具拒绝；原生关闭：`extract_path_from_arguments` → 非文本扩展名 → `reject_read_file`（双语错误注入）。
- **P3 配额拒绝（before，最先）**：quota rail（95）→ 开关关 return → 命令类名（含 `名_` 变体）→ `asyncio.to_thread(check_workspace_command_tool)` → block → `_deny`。
- **P4 归一与通知（after）**：stream `after_tool_call` → `normalize_read_file_tool_outcome`（read 类 + 非已标错 → validate → 失败改写）→ `_annotate_whitespace_path_fix`（按 tool_call_id pop fixes → 通知前置 `tool_msg.content`，仅 content 为 str 时）。
- **P5 附件物化（server 入口）**：enterprise 附件需下载 → `check_workspace_command_tool`（同步）→ block 上抛请求失败；否则 materialize 后由写侧门禁兜底。

## 异常分支与退出路径

| 分型 | 触发 | 行为 |
|---|---|---|
| 修复放弃 | N2 任一前置 / N3 非恰一匹配 | 返 None，工具按原参执行（fail-open） |
| 修参异常 | 坏 JSON / 非 dict / `AttributeError`/`TypeError`/`OSError` | None 或 `[]` + warning，原参不变 |
| quota 阻塞 | du / walk 慢 | `asyncio.to_thread` 卸载，事件循环不卡 |
| quota 满额 | status == "block" | rail：`_deny` 注入失败结果；附件入口：异常上抛请求失败 |
| quota 关闭 | env 未开 | 全放行（N12） |
| 读 before 非文本 | 扩展名命中 | 原生关闭：`_skip_tool` + 注入，工具不执行；原生开启：图片放行、非图片二进制仍拒（N13） |
| 读已提供图片（原生） | 命中 invoke 快照 | 整批拒绝（含 `file_paths` 任一项），避免重复 base64 | `test_native_mode_rejects_batch_read_when_any_path_is_provided` |
| 未提供图片 / 视觉工具（原生开/关） | 开：图片放行、视觉工具拒绝；关：视觉工具放行 | 读图通道歧义 | `test_native_mode_allows_unprovided_image_and_rejects_other_binary`、`test_native_mode_rejects_vision_tool`、`test_non_native_mode_keeps_vision_tool_and_blocks_batch_non_text` |
| 读 after 二进制误读 | 启发式命中 | 改写 tool_result / tool_msg（模型收到引导文案） |
| multimodal content | `tool_msg.content` 非 str | 不注通知（结构不破坏） |
| tool_msg 缺失 | read 注入路径 | 新建 `ToolMessage` 并补 `tool_call_id` |
| inputs 非 ToolCallInputs | 归一 / 注入路径 | 直接 return |

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| 文件名插空格 | 修复为唯一 squash 匹配 | 修错文件 | `test_file_name_with_inserted_spaces_is_resolved` |
| 目录段多层差异 | 逐段修复 | 同 | `test_directory_segments_are_resolved_too` |
| 原路径已存在 | 不动 | 误改合法路径 | `test_existing_path_is_left_alone` |
| squash 后多匹配 | 放弃（N3） | 歧义修错 | `test_ambiguous_match_is_not_resolved` |
| 非空白差异（数字 / 扩展名） | 放弃 | 同 | `test_non_whitespace_difference_is_not_resolved` |
| 相对路径无 base_dir | 放弃 | —— | `test_relative_path_uses_base_dir` |
| dict / JSON string 双形态 | 均可修且原参不变异（N4） | 原参被改破坏重放 | `test_arguments_dict_and_json_string_are_fixed_without_mutation` |
| `file_paths` 列表 | 逐条修，未命中项原样保留 | 整列表错修 | `test_file_paths_list_is_fixed_per_entry` |
| 无可修 / 坏 JSON / None | 返 None | —— | `test_nothing_to_fix_returns_none` |
| 宿主写回 arguments | 类型保持（str 保持 str） | 下游 schema 拒收 | `test_stream_event_rail_rewrites_tool_call_arguments`、`test_json_string_arguments_stay_a_json_string`、`test_hooks_keep_json_string_arguments_as_string` |
| 并行调用共享 extra | 只注自己的结果（tool_call_id pop） | 修复通知串扰 | `test_parallel_tool_calls_only_annotate_their_own_result`、`test_hooks_fix_the_path_and_annotate_only_the_corrected_call` |
| 无修正 / 非文本 content | 不注通知 | 破坏 multimodal | `test_stream_event_rail_leaves_result_alone_without_correction` |
| 英文通知 | 含原 / 纠正双路径 | —— | `test_english_notice_names_both_paths` |
| parallel_read 聚合含特征词 | 整体放行（N8） | 整批合法结果误判 | `test_parallel_read_aggregate_with_binary_marker_in_content_passes`、`test_parallel_read_aggregate_as_plain_dict_passes` |
| 单文件长文本含特征词 | 放行（词表 ≤500 门限） | 源码误判 | `test_single_file_tool_output_content_with_marker_passes` |
| 单文件 success=False | 透传自带 error | 丢失工具侧文案 | `test_single_file_tool_output_failure_uses_error` |
| 短错误消息命中词表 | 拦截 | 漏拦 | `test_short_error_message_content_still_rejected` |
| 长文本含 `\x00` | 拦截（不受长度门限） | 漏拦真实二进制 | `test_null_bytes_in_long_content_still_rejected` |
| 空结果 | 拦截并提示 | 漏拦 | `test_empty_result_still_rejected` |
| 命令工具满额 / 开关关闭 / 附件 block | `_deny` / 放行 / 请求失败 | —— | **无专属单测**（quota 模块级测试 `test_workspace_quota_async_usage.py` 归 common 域 S_07；rail 行为改动手工过场景） |

## 修改检查单

- 改三份工具名单（N1 / read 7 名 / 命令 7 名）→ 各自语义不可混（读侧修参 / read 校验 / 命令拦截）；`名_` 前缀匹配保持
- 改 `_looks_like_binary_payload`（500 词表门限 / 4096 采样 / 5% 阈值 / 控制字符区间）→ 误判与漏拦两侧用例回归——**词表与扩展名表无表驱动单测全覆盖**，增删须人工过真实文件样例
- 改 parallel_read 识别键 → 聚合放行两用例回归
- 改 `[READ_FILE_ERROR]` 前缀 → `is_read_file_error_message` 幂等判定与文案生成两端对齐；注意 S_08 裸前缀名单不认它（`[ERROR]:` 才准入），若要联动须显式注册
- 改 `_deny` 结果形态（dict / string）→ 与 read 注入的 string 形态差异是有意设计（结构化 `code`）；改前确认 UI / 上游消费方；S_08 错误检测会把该 dict 的 `success=False` 计为错误结果（熔断默认关，无实际影响）
- 改 priority（95 / 80）→ 钩子链顺序依赖（quota 先置 `_skip_tool`，stream 据此跳过 in_progress）
- 改 `check_workspace_command_tool` 语义 → rail 与 `interface.py` 附件物化**同入口复用**，两处行为联动验证
- 改 extra 键 `_jiuwenswarm_whitespace_path_fixes` → before（setdefault）/ after（pop）两端成对
- 改 N13/N14 的原生模式分支 → 判定依据全部来自 [[S_22]]（resolved `enable_read_image_multimodal` 与 `ctx.extra` 快照），不要在 rail 里另读 ContextVar 或 self 缓存；批量 `file_paths` 与单路径必须同一判定
- 动 stream_event_rail 主体 → 本 spec 的读拦截 / 空白修复 / after 归一切片不得移到 `_skip_tool` 检查之后；宿主本体改动归事件流域评审
- 改配额本体（du 缓存 / 策略 / 写侧门禁）→ 去 common 域 S_07 修改检查单，本 spec 只验证薄挂载未破坏

## 与其它 spec 的关系

- [[S_07]]（harness 域，LLM 调用层失败恢复）：可靠性域兄弟件——S_07 管**模型调用失败后的自愈**，本 spec 管**工具输入失败的预防与归一**（拦截在前、失败文案规整在后）
- [[S_08]]（工具循环熔断）：S_08 管循环态（重复 / 乒乓 / 连错），本 spec 管单次调用合法性；quota `_deny` 的结构化失败会喂给 S_08 的错误检测（默认关）
- [[S_09]]（并发安全与计划一致性）：同域兄弟件；本 spec 三件不涉工具注册
- [[S_22]]（原生多模态图片输入）：图片读取语义的唯一契约源——N13/N14 的判定依据（能力解析、已提供图片快照、`ReadFileTool` resolver）全在彼处；本 spec 只保留"何时拦截 / 拦截后注入什么"的宿主切片
- common 域 S_07（`jiuwenswarm/common/docs/specs/S_07_workspace-safety-and-quota.md`）：配额本体契约（策略 / 缓存 / 对账 / 写侧门禁）；本 spec 是其**命令工具侧 + 附件下载侧**的薄挂载，开关 / 快照 / 异常类型全部复用
- [[S_20]]（事件流主通道）：宿主 `JiuSwarmStreamEventRail` 的主体契约（发射 / sid / cwd rebind）归彼处；本 spec 的挂载点是宿主 `before_tool_call` / `after_tool_call` 的固定切片，改宿主主体时不得挪动这些切片的判定顺序
