# S_22 原生多模态图片输入

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_22 / native-multimodal-image-input |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/agents/harness/common/tools/model_capabilities.py`（能力解析纯函数）；`.../common/prompt/user_prompt_builder.py`（引用行 / 像素注入 / 视图过滤 / 预算与状态行）；`.../common/rails/multimodal_image_rail.py`（发送侧唯一决策点）；`.../common/rails/stream_event_rail.py`（工具侧读图 / 视觉工具护栏）；`.../common/rails/read_file_validation.py`（拦截文案与扩展名判定）；`.../common/rails/concurrent_safe_rails.py`（ReadFileTool resolver 装配）；`.../common/rails/progressive_tool_rail.py`（工具渐进式披露）；装配点 `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`（`_resolve_enable_read_image_multimodal` / `_build_multimodal_image_rail` / `_build_image_tool_fallback_notice` / `_multimodal_image_files` 参数归一）、`interface_code.py`、`interface_flash.py`；配置 `jiuwenswarm/resources/config.yaml`（`react.multimodal_image_*` 三预算） |
| 关联模块（agent-core 仓，跨仓契约） | `openjiuwen.harness.tools.filesystem.ReadFileTool`（`enable_image_multimodal` + `image_multimodal_resolver`，调用时求值）、`openjiuwen.harness.rails.DeepAgentRail`（`before_model_call` / `priority`）、ContextWindow `model_copy` / window mutator 机制 |
| 最近一次修订日期 | 2026-10-31 |
| 关联 feature | F_01_native-multimodal-image-input.md |

## 范围 / 边界

本 spec 管**用户上传图片如何进入模型上下文**的完整链路：上传件 → 请求消息里的引用行 →
模型能力解析（是否原生看图）→ 发送前像素物化 / 视图剥离 → 预算与降级状态行 →
工具侧护栏（禁止重复读图 / 禁止误用视觉工具）→ 失败提示一致性。

不管：

- 上传接口、文件分块传输、附件物化 → common 域 S_11 / server 附件物化链路
- 图片工具（`visual_question_answering` / `image_reading`）本身的实现 → 工具提供方
- 通用读文件二进制启发式与配额门禁 → [[S_10]]（本 spec 只定义原生模式分支的判定语义）
- 事件发射 / stream 队列 → [[S_20]]；宿主 rail 主体契约归彼处
- 压缩 / 上下文溢出恢复 → [[S_07]]（像素预算导致的裁剪不触发压缩重试）

## 不变量

- **N1（单一真源）**：一次请求"是否走原生图片输入"只在 adapter 解析一次，结果写入 `deep_config.enable_read_image_multimodal`；`MultimodalImageRail`、`ReadFileTool` resolver、stream rail 护栏、fallback notice 全部读这一个值，**不得各自重新推断模型能力**。
- **N2（模型条目是硬边界，配置只在未声明时兜底）**：解析顺序——模型条目 `supports_image=False` 或 `modalities` 不含 `image` → **False**（显式 `true` 也不能覆盖，避免把图片塞给纯文本模型导致请求必然失败）；模型条目声明支持图片 → 显式 `false` 仍可关闭（合法的 VQA 降级）；条目未声明 → 显式布尔值优先；否则查内置模型能力表（先查 text-only 前缀表，再查 image-capable 前缀表）。
- **N3（发送侧唯一决策点）**：像素是否真的发给模型，只由 `MultimodalImageRail.before_model_call` 决定——enabled 时安装"物化" window mutator（把引用行替换成 image content，写进历史）；disabled 时安装"视图过滤" mutator（只对本次发送的视图副本剥掉 image_url，历史里的 base64 原样保留，供后续切回视觉模型复用）。
- **N4（工具侧与发送侧同源）**：原生开启时，stream rail 对 read_file 的**已提供图片路径**一律拒绝（像素已在上下文，重复读只会多一份 base64）；对非图片的非文本扩展名仍拒绝；未提供的图片放行，由 `ReadFileTool` 按 resolver 内联像素。原生开启时视觉工具（`visual_question_answering` / `image_reading`）直接拒绝并提示"已提供像素"；原生关闭时视觉工具是唯一读图通道，不拦截。判定细节见 [[S_10]] N13/N14。
- **N5（预算与降级可追溯）**：单次请求的图片受三预算约束（总字节 / 视觉 token / 张数，0 表示不限），超预算按**最老优先**降级为 `[image #n omitted: reason]` 文本状态行并写入历史；成功注入的记 `[image #n attached]`。状态行是模型可见的事实来源，后续轮次据此知道"这张图当时没给 / 给过"。
- **N6（视图过滤不改历史）**：非视觉模型的剥离只发生在 `model_copy` 出的视图上，不得就地修改历史消息——否则切回视觉模型后图片永久丢失，且 checkpointer 里的原始内容被污染。
- **N7（已提供图片判定不依赖 ContextVar）**：invoke 开始时把本次请求的图片路径快照写入 `ctx.extra["_jiuwenswarm_provided_image_paths"]`；工具跨任务 / 线程执行时 ContextVar 可能丢失，护栏必须读快照（路径归一化后比较，兼容绝对 / 相对 / `..` 形态）。ContextVar（`set_current_multimodal_image_files`）只在同一调用栈内的 prompt 组装侧使用。
- **N8（提示一致性）**：仅当解析结果为 False 且请求确实带图片引用时，才向用户发"已切换为图片工具"提示；解析为 True 时不得再发该提示（否则与"像素已直接发送"自相矛盾）。系统提示里的 `multimodal_image_policy` 段同样按解析值二选一。
- **N9（失败降级不阻断）**：单张图片读取 / 解码 / 缩放失败时跳过该图并记状态行，不让整个请求失败；rail 构建异常降级为 None（装配失败不阻断 agent 创建）。

## 接口契约

### model_capabilities（纯函数）

| 成员 | 契约 |
|---|---|
| `builtin_model_supports_image(model_name)` | 先查 text-only 前缀（`o3-mini` / `o3-pro`），再查 image-capable 前缀；未知模型 → False |
| `resolve_model_entry_support(modalities, supports_image)` | `supports_image` 为 bool → 直接返回；否则 `modalities` 非 None → `"image" in modalities`；两者都没有 → None（未声明） |
| `resolve_model_supports_image(model_name, modalities, supports_image)` | 条目声明优先，未声明回落内置表 |

### user_prompt_builder（引用行 / 像素 / 预算）

| 成员 | 契约 |
|---|---|
| `build_image_reference_line(image_file, index)` | 生成 `[image #n] path=... filename=... mime=... size=...`；路径含空格也按行解析（正则见 `_IMAGE_REFERENCE_LINE_RE`） |
| `parse_image_references(text)` / `collect_image_references_from_messages(messages)` | 从历史文本反解引用，供物化 mutator 定位"哪些消息要换成像素" |
| `inject_image_content_from_references(messages, ...)` | 读文件 → 校验 MIME / 尺寸 → 缩放到 `_MAX_VISION_INLINE_IMAGE_DIMENSION` 内并按质量阶梯压到字节预算 → 生成 data URL → 就地把引用行所在消息改成多模态 content；超预算 / 失败按 N5 写状态行 |
| `filter_image_content_from_view(messages)` | N6：返回视图副本 + 被剥离块数；历史不动 |
| `ensure_multimodal_image_window_mutator(context, ...)` / `ensure_multimodal_image_filter_window_mutator(context)` | 在 ContextWindow 上安装物化 / 过滤 mutator；属性名 `_jiuwenswarm_multimodal_image_window_mutator` 防重复安装 |
| `estimate_image_visual_tokens(width, height)` | 512px tile 计费：85 + 170 × tiles，用于 token 预算 |
| `normalize_image_path(path)` / `is_provided_image_path(path, provided_paths)` | N7 的路径归一与快照比较；空路径 / 空快照一律 False |
| `set_current_multimodal_image_files` / `current_multimodal_image_files` / `reset_current_multimodal_image_files` | ContextVar 版"本次请求图片列表"，仅同调用栈使用；跨任务判定走 N7 快照 |
| `strip_image_content_blocks(content)` | 纯函数：剥掉 content 里的 image 块并保留文本，返回 `(new_content, removed)`；供视图过滤复用 |

### MultimodalImageRail（`DeepAgentRail`，priority 75）

| 成员 | 契约 |
|---|---|
| `__init__(enable_image_multimodal=None, *, max_total_bytes=0, max_visual_tokens=0, max_count=0)` | 显式值优先；None 时在 `before_model_call` 读 `deep_config.enable_read_image_multimodal`（N1） |
| `before_model_call(ctx)` | N3：enabled → 物化 mutator；disabled → 过滤 mutator；`ctx.context is None` 直接返回；同时刷新系统提示 `multimodal_image_policy` 段（N8） |

### stream_event_rail / read_file_validation（工具侧）

| 成员 | 契约 |
|---|---|
| `_native_read_image_enabled()` | 读 `deep_config.enable_read_image_multimodal`；异常 / 缺失 → False |
| `_provided_image_paths(ctx)` / `_cache_provided_image_paths(ctx)` | N7 快照：优先 `ctx.extra`，缺失时从 ContextVar 懒缓存一次写回 |
| `build_provided_image_read_error(path, language)` | `[READ_FILE_ERROR]` + "已随消息提供像素，直接使用" |
| `build_native_image_tool_error(language)` | `[VISION_TOOL_SKIPPED]` + "不要调用视觉工具" |
| `build_non_text_read_error(path, language)` | 图片 / 二进制分型引导（[[S_10]] N6） |

### adapter（解析与提示）

| 成员 | 契约 |
|---|---|
| `_resolve_enable_read_image_multimodal(deep_config_dict, model)` | N2 三态解析；模型条目声明 > 显式配置 > 内置表 |
| `_build_image_tool_fallback_notice(...)` | N8：解析结果 True → None；False 且请求带图片引用 → 提示 |
| `_build_multimodal_image_rail(enable, *, budgets)` | 构造失败 warning + None（N9） |

## 数据结构

- 引用行：`[image #n] path=<绝对路径> filename=<名> mime=<MIME> size=<bytes>`（`n` 从 1 起，按请求内顺序）
- 状态行：`[image #n attached]` / `[image #n omitted: <reason>]`（reason 含预算 / 读取失败 / 尺寸非法等）
- 图片项：`{"path": str, "filename": str, "mime_type": str, "size": int}`；`extract_multimodal_image_files(params)` 从请求参数归一
- `ctx.extra` 键：`"_jiuwenswarm_provided_image_paths"`（invoke 级快照，list[str]，路径已归一）
- ContextVar：`_CURRENT_MULTIMODAL_IMAGE_FILES`（tuple[dict]，同调用栈生效，用完 reset）
- 配置：`react.multimodal_image_budget`（总字节，0 不限，默认 0）、`react.multimodal_image_token_budget`（视觉 token，0 不限，默认 0）、`react.multimodal_image_max_count`（张数，默认 8）
- 常量：`_MAX_VISION_INLINE_IMAGE_BYTES`（10MB 单图上限）、`_MAX_VISION_INLINE_IMAGE_DIMENSION`（1536px）、质量阶梯 95/85/70/50/30/10、`_SHRINK_SCALE_STEP=0.85`、`_MIN_SHRINK_DIMENSION=32`

## 执行路径全景

- **P1 上传 → 引用行**：请求携带 `_multimodal_image_files` → adapter 归一并传给 prompt 组装 → 在用户消息末尾追加引用行 → ContextVar 记录本次图片列表（同调用栈）。
- **P2 能力解析（adapter）**：取模型条目 `modalities` / `supports_image` → N2 → 写 `deep_config.enable_read_image_multimodal` → `create_deep_agent` 与 `MultimodalImageRail` 收到同一值。
- **P3 发送前决策（rail 75）**：`before_model_call` → N3 安装 mutator → 系统提示策略段按解析值二选一；enabled 时 mutator 在窗口物化阶段执行 P4，disabled 时执行 P5。
- **P4 像素物化**：`collect_image_references_from_messages` → 逐图读文件 / 校验 / 缩放压缩 → 预算检查（最老优先，N5）→ 就地写多模态 content + 状态行 → 历史持久化后后续轮次复用，不重复读文件。
- **P5 视图剥离**：`filter_image_content_from_view` 在发送视图副本上剥 image 块 → `strip_image_content_blocks` 保留文本 → 历史 base64 不动（N6）。
- **P6 工具护栏（before_tool_call）**：读 `deep_config` 解析值 → N4 三分支（已提供图片拒绝 / 非图片二进制拒绝 / 图片放行）；视觉工具在原生开启时拒绝。
- **P7 失败提示**：`_build_image_tool_fallback_notice` 仅在解析 False 时发"已切换为工具"提示（N8）；系统提示策略段与 rail 决策同源。
- **P8 配置读取**：adapter 读 `react.multimodal_image_*` → 传给 rail 构造；0 值表示不限。

## 异常分支与退出路径

| 分型 | 触发 | 行为 |
|---|---|---|
| 模型条目明确不支持 + 显式 true | 用户配置与模型能力冲突 | 解析 False（N2），走工具链，不再发矛盾提示（N8） |
| 图片文件读取失败 / MIME 不支持 | 文件被删 / 非图片 | 跳过该图，写 `omitted` 状态行；其余图片继续 |
| 单图超 10MB / 解码失败 | 超大图 / 损坏文件 | 跳过并写状态行，不阻断请求 |
| 超总字节 / token / 张数预算 | 多图大图 | 最老优先降级为 `omitted` 状态行（N5） |
| `ctx.context is None` | rail 在无窗口场景调用 | 直接 return，不安装 mutator |
| rail 构造失败 | 依赖缺失 / 版本不匹配 | warning + None，agent 继续装配（N9） |
| 工具跨线程执行导致 ContextVar 丢失 | 工具在线程池 / 新任务运行 | 走 `ctx.extra` 快照（N7），不误判"图片未提供" |
| 已提供图片被 read_file 重复读取 | 模型不知道像素已在上下文 | 整批拒绝并提示直接使用像素（N4 / [[S_10]] N13） |
| 原生开启时调用视觉工具 | 模型习惯性调用 | 拒绝并提示不要调用（N4 / [[S_10]] N14） |

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| 视觉模型 + 未配置开关 | 内置表命中 → 原生开启 | 图片没发出去 | `test_unset_entry_falls_back_to_builtin_table` |
| 纯文本模型 + 显式 true | 解析 False | 请求必然失败 | `test_explicit_true_cannot_override_text_only_model_entry` |
| 视觉模型 + 显式 false | 解析 False（VQA 降级） | 用户主动选择被忽略 | `test_explicit_false_can_disable_image_capable_model_entry` |
| 条目 `supports_image=False` + 显式 true | 解析 False | 配置误覆盖硬边界 | `test_supports_image_false_entry_blocks_explicit_true` |
| 内置表宽前缀 | `o1` / `llama-3.2-1b` / `mistral-small-3` 不再判视觉 | 误判纯文本模型 | `test_builtin_table_no_longer_matches_text_only_variants` |
| 非视觉模型历史含 base64 | 只剥视图，历史保留 | 切回视觉模型丢图 | `test_filter_image_content_from_view_strips_image_blocks_keeps_original` |
| 超张数 / token / 字节预算 | 最老优先 `omitted` | 预算失控 | `test_inject_image_content_drops_oldest_new_image_when_over_count_budget` 等 |
| 已提供图片重复读（单路径 / 批量） | 整批拒绝 | 重复 base64 膨胀上下文 | `test_native_mode_rejects_batch_read_when_any_path_is_provided` |
| 未提供图片读（原生） | 放行给 ReadFileTool | 图片读不到 | `test_native_mode_allows_unprovided_image_and_rejects_other_binary` |
| 原生开启调视觉工具 | 拒绝并提示 | 多绕一圈工具 | `test_native_mode_rejects_vision_tool` |
| 原生关闭调视觉工具 | 放行 | VQA 通道被误拦 | `test_non_native_mode_keeps_vision_tool_and_blocks_batch_non_text` |
| 工具跨任务执行（ContextVar 丢） | `ctx.extra` 快照仍命中 | 误判未提供、重复读图 | `test_provided_image_paths_snapshot_survives_contextvar_reset` |
| 解析 False + 请求带图 | 发"已切换为工具"提示 | 提示与事实矛盾 | `test_forced_native_path_does_not_claim_tool_fallback` 等 |
| 模型热切换 | resolver 调用时求值 | read_file 行为冻结在 init | `test_read_tool_resolver_follows_model_switch` |
| rail 显式开关 vs deep_config | 显式值优先 | 装配语义漂移 | `test_explicit_rail_flag_wins_over_deep_config` |

## 修改检查单

- 改能力解析（内置表 / 条目三态）→ 同步 adapter `_resolve_enable_read_image_multimodal`、`model_capabilities` 三函数与单测；新增视觉模型前缀必须窄匹配（禁止 `o1` 这类宽前缀），text-only 前缀表优先于 image 表
- 改 `deep_config.enable_read_image_multimodal` 的写入点 → N1 单一真源，rail / resolver / stream 护栏 / notice 四处必须同源；不要在 rail 或工具里重新推断模型能力
- 改发送侧决策 → `MultimodalImageRail.before_model_call` 是唯一入口；新增分支必须同时覆盖 enabled / disabled 两种 mutator，且 disabled 只能是视图级（N6）
- 改预算 / 降级顺序 → 保持"最老优先"与状态行可追溯（N5）；0 语义是不限，不能当"禁用"用
- 改已提供图片判定 → 只读 `ctx.extra` 快照（N7）；快照写入点在 `before_invoke`，新增读取方不得依赖 ContextVar
- 改工具护栏 → 同步 [[S_10]] N13/N14 与 `build_provided_image_read_error` / `build_native_image_tool_error`；批量 `file_paths` 必须与单路径同判定
- 改 fallback notice → 只在解析 False 时发（N8）；同时检查系统提示 `multimodal_image_policy` 段与 `ProgressiveToolRail` 的工具披露
- 改 ReadFileTool 构造 → `ConcurrentSafeSysOperationRail.init` 必须同时传 `enable_image_multimodal` 与 `image_multimodal_resolver`；resolver 读 rail 显式值 → `deep_config` → False
- 改上传 / 引用行格式 → `_IMAGE_REFERENCE_LINE_RE` / `parse_image_references` / `build_image_reference_line` 三处同改；旧历史里的旧格式必须仍可解析

## 与其它 spec 的关系

- [[S_10]]（输入侧守卫）：本 spec 是其 N13/N14 的语义来源；S_10 保留"何时拦截、注入什么文案"的宿主切片，不复制能力解析与预算细节
- [[S_20]]（事件流主通道）：宿主 `JiuSwarmStreamEventRail` 的发射 / sid / cwd 契约归彼处；本 spec 只占用其 `before_tool_call` 的读图分支与 `before_invoke` 的快照缓存点
- [[S_09]]（并发安全）：`ConcurrentSafeSysOperationRail.init` 的 ReadFileTool resolver 装配属本 spec N1/N4 的落地，注册安全仍归 S_09
- [[S_07]]（LLM 调用层失败恢复）：像素预算造成的裁剪不触发溢出恢复；本 spec 的降级发生在发送前，不应与 413 恢复链混用
- `jiuwenswarm/server/runtime/agent_adapter/AGENTS.md`：适配器层"模型缓存 / fallback notice / rail 构建"的散件说明在彼处；本 spec 登记其解析契约（N1/N2/N8），不复制适配器其它铁律
- `jiuwenswarm/common/AGENTS.md`（其 S_11 文件传输 / S_13 请求上下文）：上传件来源与 `_multimodal_image_files` 参数透传归彼处；本 spec 从"引用行已生成"开始
