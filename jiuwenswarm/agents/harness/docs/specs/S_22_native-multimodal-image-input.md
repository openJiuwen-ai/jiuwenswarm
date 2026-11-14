# S_22 原生多模态图片输入

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_22 / native-multimodal-image-input |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/agents/harness/common/tools/model_capabilities.py`（能力解析纯函数）；`.../common/multimodal/`（`image_reference.py` 引用行 / 上传清单通道，`image_history.py` 像素物化 / 视图过滤 / 预算与状态行，`image_processing.py` 缩放编码）；`.../common/rails/multimodal_image_rail.py`（发送侧唯一决策点）；`.../common/rails/stream_event_rail.py`（工具侧读图 / 视觉工具护栏）；`.../common/rails/read_file_validation.py`（拦截文案与扩展名判定）；`.../common/rails/concurrent_safe_rails.py`（ReadFileTool resolver 装配）；`.../common/rails/progressive_tool_rail.py`（工具渐进式披露）；team 通道 `jiuwenswarm/agents/swarm/config_specs.py`（成员能力位 / 成员 rail 声明）与 `jiuwenswarm/agents/swarm/providers/multimodal.py`（`swarm.multimodal_image` provider）；装配点 `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`（`_resolve_enable_read_image_multimodal` / `_build_multimodal_image_rail` / `_build_image_tool_fallback_notice` / `_multimodal_image_files` 参数归一）与 `.../agent_adapter/team_helpers.py`（team 轮次清单写入 build context）；`interface_code.py`、`interface_flash.py`；配置 `jiuwenswarm/resources/config.yaml`（`react.multimodal_image_*` 三预算） |
| 关联模块（agent-core 仓，跨仓契约） | `openjiuwen.harness.tools.filesystem.ReadFileTool`（`enable_image_multimodal` + `image_multimodal_resolver`，调用时求值；`READ_IMAGE_MAX_BYTES` / `READ_IMAGE_MAX_DIMENSION` 为可覆盖的类属性）、`openjiuwen.harness.rails.DeepAgentRail`（`before_model_call` / `priority`）、`openjiuwen.harness.image_modality_probe`（`get_declared_image_support` 读 `ModelClientConfig.supports_vision`）、ContextWindow `model_copy` / window mutator 机制；team 侧 `openjiuwen.agent_teams.agent.agent_configurator`（成员 `enable_read_image_multimodal` None→False 兜底）、`openjiuwen.agent_teams.harness.TeamHarness`（每轮按 build context 重建 NativeHarness） |
| 最近一次修订日期 | 2026-11-06 |
| 关联 feature | B_05_team-native-multimodal-image-input.md（bugfix） |

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
- **N2（模型条目是唯一依据）**：解析顺序——模型条目 `modalities` 含 `image` → **True**；未声明 / 不含 `image` / 其它不可识别值 → **False**（走 VQA / 视觉工具链）。不再内置模型能力表、不按模型名前缀猜测，也不接受单独的布尔开关；`coerce_modalities` 只负责把 YAML 里的列表或逗号 / 空白分隔字符串归一成规范小写列表。agent-core 另提供 `ModelClientConfig.supports_vision` 作为探针前置兜底（`get_declared_image_support`），但仅在 `deep_config.enable_read_image_multimodal` 尚未写入时生效；jiuwenswarm 链路恒由 `modalities` 解析并显式写入该值，故不构成第二真源。
- **N3（发送侧唯一决策点）**：像素是否真的发给模型，只由 `MultimodalImageRail.before_model_call` 决定——enabled 时安装"物化" window mutator（把引用行替换成 image content，写进历史）；disabled 时安装"视图过滤" mutator（只对本次发送的视图副本剥掉 image_url，历史里的 base64 原样保留，供后续切回视觉模型复用）。
- **N4（工具侧与发送侧同源）**：原生开启时，stream rail 对 read_file 的**已提供图片路径**一律拒绝（像素已在上下文，重复读只会多一份 base64）；对非图片的非文本扩展名仍拒绝；未提供的图片放行，由 `ReadFileTool` 按 resolver 内联像素。原生开启时视觉工具（`visual_question_answering` / `image_reading`）直接拒绝并提示"已提供像素"；原生关闭时视觉工具是唯一读图通道，不拦截。判定细节见 [[S_10]] N13/N14。
- **N5（预算与降级可追溯）**：单次请求的图片受三预算约束（总字节 / 视觉 token / 张数，0 表示不限），超预算按**最老优先**降级为 `[image #n omitted: reason]` 文本状态行并写入历史；成功注入的记 `[image #n attached]`。状态行是模型可见的事实来源，后续轮次据此知道"这张图当时没给 / 给过"。两类状态行都是**终态**：同一消息内 `attached` / `omitted`（含 `omitted: duplicate`）对应的路径，成员确定性分发一律不再补引用行、不再重复读盘物化——预算淘汰 / 读取失败的图不会在每次 LLM 迭代（每轮 `before_model_call`）被重新补行、重新缩放编码，否则上下文无界膨胀。
- **N6（视图过滤不改历史）**：非视觉模型的剥离只发生在 `model_copy` 出的视图上，不得就地修改历史消息——否则切回视觉模型后图片永久丢失，且 checkpointer 里的原始内容被污染。
- **N7（已提供图片判定不依赖 ContextVar）**：invoke 开始时把本次请求的图片路径快照写入 `ctx.extra["_jiuwenswarm_provided_image_paths"]`；工具跨任务 / 线程执行时 ContextVar 可能丢失，护栏必须读快照（路径归一化后比较，兼容绝对 / 相对 / `..` 形态）。ContextVar（`set_current_multimodal_image_files`）只在同一调用栈内的 prompt 组装侧使用。
- **N8（提示一致性）**：仅当解析结果为 False 且请求确实带图片引用时，才向用户发"已切换为图片工具"提示；解析为 True 时不得再发该提示（否则与"像素已直接发送"自相矛盾）。系统提示里的 `multimodal_image_policy` 段同样按解析值二选一。
- **N9（失败降级不阻断）**：单张图片读取 / 解码 / 缩放失败时跳过该图并记状态行，不让整个请求失败；rail 构建异常降级为 None（装配失败不阻断 agent 创建）。
- **N10（team 成员同源）**：team 模式下**每个成员**（leader / teammate / 命名成员）都挂 `swarm.multimodal_image` rail，三要素必须成对存在——(a) 成员的 `DeepAgentSpec.enable_read_image_multimodal` 按**该成员模型**的 `modalities` 显式写 bool（成员 None 会被 SDK 固化为 False）；成员 spec 指定的模型名按条目 `model_client_config.model_name` 或 `alias` 命中，查不到条目按 N2 判 False 而**不回落**默认条目的能力位；(b) 成员挂 `swarm.multimodal_image` rail（provider 构造 `MultimodalImageRail`）；(c) 本轮上传清单经 `BuildContext.request_metadata["multimodal_image_files"]` 到达 rail（adapter 的 `self._instance` 属性镜像与请求 ContextVar 都到不了成员任务）。live build context 的清单绑定只发生在**确实会开新一轮**的请求：控制续写（权限 / ask_user 作答，`reuse_active_waiter`）只向既有 waiter 投递作答、adapter 传 `image_files=[]`，不得覆盖写；slash 命令早退同样不写。否则"上传图片 → 权限确认 → 继续"会清空在该轮生效的 live 清单，本轮后续新建的成员 harness / 上下文溢出重建读到空清单，`[image #n]` 被判不在清单内丢弃。绑定位置固定在 slash 早退之后、投递 query（`team_manager.interact` / `_start_team_stream_round`）之前——`TeamHarness` 每轮按同一 build context 重建 NativeHarness，投递后再写赶不上本轮成员的 rail 构造。授权边界在物化处而不在挂载处：只有**清单内路径**且出现在该成员**自己历史中的 user-authored 引用行**才出图（任务看板正文 / 工具结果里的路径不物化）；leader 把图片交给成员时须用 `send_message` 原样转述 `[image #n] path=...` 引用行，leader rail 的 `dispatch_hint` 负责把这条规则写进系统提示；并且 leader rail 在 `before_tool_call` 对 `send_message` 做**确定性兜底**——消息正文若提到本轮上传图的文件名或完整路径，自动在正文末尾补对应引用行（广播 `to="*"` / 回用户 `to="user"` 不补，单条最多补 4 张），避免模型只写任务看板或只写占位符时成员仍拿不到像素。成员侧再兜一层**确定性分发**：成员 rail 在 `before_model_call` 扫描自身上下文（含任务看板 / 工具结果等任意角色的文本）是否提到清单图（文件名或完整路径），命中且该路径**未处理**（`attached` 与 `omitted` 都是终态，见 N5）时主动把引用行补到最近一条 user 消息再物化；清单图全部已处理时直接返回，不再对全部消息做归一扫描。"提到清单图"的判定是**带边界的 token 匹配**：路径 / 文件名先统一分隔符（`\` → `/`）并忽略大小写，命中片段前后不得是 `[0-9A-Za-z_-]`（正则 lookaround），因此 `1.png` 不命中 `11.png` / `data.png`、`C:\Uploads\3.JPG` 能命中清单里的 `C:/uploads/3.jpg`、`请识别3.jpg` 仍命中；leader 分发兜底与成员确定性分发共用 `image_reference` 的同一套判定（不是裸 `in` 子串）。因此"模型声明原生能力 + 图在本轮上传清单内"时不由 leader 的消息格式决定是否出图；像素一旦提供，既有护栏会拒绝成员对同一图的 VQA/OCR（N4）。成员还有一条**超时/跨轮可用的模型可见路径**：原生开启时 `ReadFileTool` 的 resolver 每次调用读 `deep_config.enable_read_image_multimodal`，`read_file(本地图片路径)` 会把图片以图像内容返回原生模型（S_22 N4 的"未提供图片放行"分支）；成员 rail 在 `enabled` 且本轮 0 像素时写策略段引导"本地路径优先 read_file，不要 VQA/OCR"，且 read_file 返回的图片块会计入下一次 `native_images_sent`，从而继续拦住 VQA。清单是请求级的：进程内成员共享同一 `request_metadata` dict（跨轮刷新可见），子进程 / 分布式成员取 spawn 时的 seed 快照（不随后续轮次刷新，见遗留）。

## 接口契约

### model_capabilities（纯函数）

| 成员 | 契约 |
|---|---|
| `coerce_modalities(value)` | 把配置里的 `modalities` 归一成小写去重列表；支持列表 / 元组 / 集合 / 逗号或空白分隔字符串；空值与非字符串容器 → None（未声明） |
| `supports_native_image(value)` | 仅当归一后的列表含 `image` 才为 True；None / 空 / 纯文本 → False |

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
| `normalize_image_mention_text(text)` / `normalized_text_mentions_image(image, normalized_text)` / `text_mentions_image(image, text)` | 图片提及判定单源（N10）：`normalize_image_mention_text` 只做分隔符 `\` → `/` + 小写（不用 `normalize_image_path`——realpath 会改写正文片段，且 POSIX 的 `os.path.normcase` 恒等，对齐不了 Windows 反斜杠形态）；`normalized_text_mentions_image` 在**已归一**文本上对"完整路径 / basename"做 lookaround 边界匹配（前后不得是 `[0-9A-Za-z_-]`），供批量场景复用一次归一；`text_mentions_image` 是单条文本的便捷封装（内部归一一次） |
| `set_current_multimodal_image_files` / `current_multimodal_image_files` / `reset_current_multimodal_image_files` | ContextVar 版"本次请求图片列表"，仅同调用栈使用；跨任务判定走 N7 快照 |
| `store_team_request_image_files(metadata, files)` / `team_request_image_files(metadata)` | N10 team 通道：覆盖写 / 读 `BuildContext.request_metadata` 的清单；空列表是"本轮无上传"的权威事实，未写入返回 None（调用方回退 agent 属性 / ContextVar）；读侧返回副本 |
| `strip_image_content_blocks(content)` | 纯函数：剥掉 content 里的 image 块并保留文本，返回 `(new_content, removed)`；供视图过滤复用 |

### swarm 装配（team 成员）

| 成员 | 契约 |
|---|---|
| `_resolve_member_native_image_enabled(config, base_spec)`（`swarm/config_specs.py`） | N2 的成员侧落地：只读**原始 config 的模型条目声明**（`models.defaults` 非空 list 优先，否则 `models.default` 单 dict；两者都没有 → False），**不经 `get_default_models`**——不做 `deepcopy`、不解密 `api_key`（team 装配按「每个成员、每一轮」调用，且本判定只要 `model_name` / `alias` / `modalities` 三个声明字段）。成员 spec 指定模型名时按条目 `model_client_config.model_name` **或**顶层 `alias` 命中，两者都未命中 → `logger.warning`（带成员模型名）+ **False，不回落默认条目**；未指定模型 → `is_default is True` 的条目，否则第一个条目；未声明 / 不含 `image` → False；显式 `base_spec.enable_read_image_multimodal` 优先（不覆盖用户配置） |
| `_multimodal_image_rail_params(config, enabled)` | 把能力位与 `react.multimodal_image_*` 三预算烘进 `RailSpec.params` |
| `ensure_manifest_images_referenced(messages, image_files, *, max_images=4)` | 成员侧确定性分发：扫描全部消息文本（含 tool result，仅用于判断"提到了哪张图"），按边界判定命中清单文件名 / 完整路径且该路径**未处理**（`attached` ∪ `omitted` 都是终态，见 N5）时，把引用行补到最近一条 user 消息末尾；幂等；**清单图全部已处理 → 直接返回 0**（不做全量文本扫描）；每条消息文本只归一一次供多张图复用；返回补了几张 |
| `MultimodalImageRail.before_model_call(ctx)` | enabled 分支在注入前调用上面的函数（仅绑定清单的成员、非 leader 的 `dispatch_hint` 实例），补完引用后交给 `inject_image_content_from_references` 统一物化 |
| `augment_message_with_reference_lines(content, image_files, *, max_images=4)` | leader 分发兜底纯函数：按文件名 / 完整路径把消息里提到的上传图补成引用行；提及判定复用 `text_mentions_image`（分隔符 / 大小写归一 + 边界对齐，`1.png` 不会被 `11.png` / `data.png` 误命中）；已有同路径引用行不重复，返回新 content |
| `MultimodalImageRail.before_tool_call(ctx)` | 仅 `dispatch_hint` 的 leader 生效：`send_message` 且收件人为具体成员时，用上面的纯函数改写 `tool_call.arguments["content"]`（同步写回 `ctx.inputs.tool_args`）；广播 / user / 非 send_message 不改写；异常只降级不阻断 |
| `swarm.multimodal_image`（`swarm/providers/multimodal.py`） | 由 config_specs 为**每个成员**声明（leader 额外带 `dispatch_hint=True`）；provider 从 `context.request_metadata` 取本轮清单，构造 `MultimodalImageRail(enable_image_multimodal=..., budgets..., image_files=..., dispatch_hint=...)` |
| `team_helpers` 轮次绑定 | `request_metadata` 覆盖写（首轮 / 冷恢复随 fresh spec 生效）+ 写进 live team agent 的 `harness.build_context.request_metadata`（跨轮复用 runtime 每轮据此重建 NativeHarness）。后者**只在确实会开新一轮的请求**写：slash 命令早退之后、投递 query（`team_manager.interact` / `_start_team_stream_round`）之前，且 `reuse_active_waiter` 为假；控制续写（权限 / ask_user 作答，adapter 传 `image_files=[]`）与 slash 请求不得覆盖写，否则清空在途图片轮清单（N10） |

### MultimodalImageRail（`DeepAgentRail`，priority 82）

| 成员 | 契约 |
|---|---|
| `__init__(enable_image_multimodal=None, *, max_total_bytes=0, max_visual_tokens=0, max_count=0, image_files=None, dispatch_hint=False)` | 显式值优先；None 时在 `before_model_call` 读 `deep_config.enable_read_image_multimodal`（N1）；`image_files` 非 None 时装配期绑定本轮清单（team 成员，N10），空列表不回退 agent 属性；`dispatch_hint=True`（leader）在原生策略段追加"用 send_message 转述引用行"的提示 |
| `_resolve_request_image_files()` | 顺序：装配期绑定清单 → agent 实例镜像（单 agent 旧行为）→ `None`（inject 回退 ContextVar） |
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
| `_resolve_enable_read_image_multimodal(deep_config_dict, model)` | N2：只读 `model.modalities`（`coerce_modalities` 归一后是否含 `image`）；`model` 缺失 → False |
| `_build_image_tool_fallback_notice(...)` | N8：解析结果 True → None；False 且请求带图片引用 → 提示 |
| `_build_multimodal_image_rail(enable, *, budgets)` | 构造失败 warning + None（N9） |

## 数据结构

- 引用行：`[image #n] path=<绝对路径> filename=<名> mime=<MIME> size=<bytes>`（`n` 从 1 起，按请求内顺序）
- 状态行：`[image #n attached]` / `[image #n omitted: <reason>]`（reason 含预算 / 读取失败 / 尺寸非法等）
- 图片项：`{"path": str, "filename": str, "mime_type": str, "size": int}`；`extract_multimodal_image_files(params)` 从请求参数归一
- `ctx.extra` 键：`"_jiuwenswarm_provided_image_paths"`（invoke 级快照，list[str]，路径已归一）
- ContextVar：`_CURRENT_MULTIMODAL_IMAGE_FILES`（tuple[dict]，同调用栈生效，用完 reset）
- `BuildContext.request_metadata` 键：`"multimodal_image_files"`（team 轮次上传清单，list[dict]；team_helpers 覆盖写、成员侧 provider 读；随 `build_context_seed` 跨序列化边界）——覆盖写分两处：fresh spec 的 metadata（首轮 / 冷恢复）与 live team agent 的 `harness.build_context`（仅确实会开新一轮的请求，slash 早退后、投递前）
- 配置：`react.multimodal_image_budget`（总字节，0 不限，默认 0）、`react.multimodal_image_token_budget`（视觉 token，0 不限，默认 0）、`react.multimodal_image_max_count`（张数，默认 8）
- 常量：`_MAX_VISION_INLINE_IMAGE_BYTES`（10MB 单图上限）、`_MAX_VISION_INLINE_IMAGE_DIMENSION`（2048px）、质量阶梯 95/85/70/50/30/10、`_SHRINK_SCALE_STEP=0.85`、`_MIN_SHRINK_DIMENSION=32`

## 执行路径全景

- **P1 上传 → 引用行**：请求携带 `_multimodal_image_files` → adapter 归一并传给 prompt 组装 → 在用户消息末尾追加引用行 → ContextVar 记录本次图片列表（同调用栈）。
- **P2 能力解析（adapter）**：取模型条目 `modalities` → `supports_native_image` → N2 → 写 `deep_config.enable_read_image_multimodal` → `create_deep_agent` 与 `MultimodalImageRail` 收到同一值。
- **P3 发送前决策（rail 82）**：`before_model_call` → N3 安装 mutator → 系统提示策略段按解析值二选一；enabled 时 mutator 在窗口物化阶段执行 P4，disabled 时执行 P5。
- **P4 像素物化**：`collect_image_references_from_messages` → 逐图读文件 / 校验 / 缩放压缩 → 预算检查（最老优先，N5）→ 就地写多模态 content + 状态行 → 历史持久化后后续轮次复用，不重复读文件。
- **P5 视图剥离**：`filter_image_content_from_view` 在发送视图副本上剥 image 块 → `strip_image_content_blocks` 保留文本 → 历史 base64 不动（N6）。
- **P6 工具护栏（before_tool_call）**：读 `deep_config` 解析值 → N4 三分支（已提供图片拒绝 / 非图片二进制拒绝 / 图片放行）；视觉工具在原生开启时拒绝。
- **P7 失败提示**：`_build_image_tool_fallback_notice` 仅在解析 False 时发"已切换为工具"提示（N8）；系统提示策略段与 rail 决策同源。
- **P8 配置读取**：adapter 读 `react.multimodal_image_*` → 传给 rail 构造；0 值表示不限。
- **P9 team 成员通道（N10）**：adapter team 分支 `_prepare_multimodal_image_inputs` 生成引用行并 `pop` 出清单 → 作为 `image_files` 参数传给 `process_team_message_stream` → 覆盖写 `request_metadata["multimodal_image_files"]`（首轮 / 冷恢复随 fresh spec 的 build context 生效）→ 同一清单写进 live team agent 的 `harness.build_context.request_metadata`（跨轮复用 runtime 每轮据此重建 NativeHarness；进程内 teammate 共享同一 dict；**只在该请求确实会开新一轮时写**——slash 早退之后、投递 query 之前、`reuse_active_waiter` 为假，控制续写 / slash 命令不覆盖写）→ `swarm.multimodal_image` provider 为每个成员构造 `MultimodalImageRail(image_files=清单)` → 成员的 `before_model_call` 按 P3/P4 物化像素；SDK 侧 `enable_read_image_multimodal` 由 `config_specs` 按成员模型 `modalities` 显式写入。leader rail 的 `dispatch_hint` 在原生策略段追加"交给成员时用 send_message 保留引用行"的规则；`before_tool_call` 再对 `send_message` 做确定性兜底（正文提到上传图文件名 / 路径 → 自动补引用行）；成员 rail 在 `before_model_call` 还会扫描自身上下文任意位置的清单图提及并补引用行（`ensure_manifest_images_referenced`，含任务看板 tool result 文本；边界归一判定、`attached` ∪ `omitted` 终态、清单全处理即早退），成员只在自身历史出现该引用行时才物化对应上传图。像素提供后，stream 护栏按 N4 拒绝同一图的 VQA/OCR。成员 `enabled` 且 0 像素时另写 `multimodal_image_policy` 策略段引导"本地路径用 read_file 原生读图"；`read_file` 命中 resolver 后图片块进入历史，下一次模型调用 `native_images_sent>0`，同样触发 VQA/OCR 拦截。

## 异常分支与退出路径

| 分型 | 触发 | 行为 | 测试覆盖 |
|---|---|---|---|
| 模型条目 `modalities` 不含 `image`（含未声明） | 纯文本模型 / 漏配 | 解析 False（N2），走工具链，不发矛盾提示（N8） | — |
| 图片文件读取失败 / MIME 不支持 | 文件被删 / 非图片 | 跳过该图，写 `omitted` 状态行；其余图片继续 | — |
| 单图超 10MB / 解码失败 | 超大图 / 损坏文件 | 跳过并写状态行，不阻断请求 | — |
| 超总字节 / token / 张数预算 | 多图大图 | 最老优先降级为 `omitted` 状态行（N5） | — |
| `ctx.context is None` | rail 在无窗口场景调用 | 直接 return，不安装 mutator | — |
| rail 构造失败 | 依赖缺失 / 版本不匹配 | warning + None，agent 继续装配（N9） | — |
| 工具跨线程执行导致 ContextVar 丢失 | 工具在线程池 / 新任务运行 | 走 `ctx.extra` 快照（N7），不误判"图片未提供" | — |
| 已提供图片被 read_file 重复读取 | 模型不知道像素已在上下文 | 整批拒绝并提示直接使用像素（N4 / [[S_10]] N13） | — |
| 原生开启时调用视觉工具 | 模型习惯性调用 | 拒绝并提示不要调用（N4 / [[S_10]] N14） | — |
| team leader 模型不含 `image` | 纯文本 leader + 用户传图 | 能力位 False → rail 走视图过滤；图片仍可经 VQA 工具识别，不报请求失败（N2/N10） | — |
| team 非首轮上传 | 复用中的 team runtime 收到新图 | 该请求确实开新一轮时，live build context 在投递前被覆盖写，下一轮 NativeHarness 重建时 rail 拿到新清单（N10）；漏写即静默 0 张 | — |
| 控制续写（权限 / ask_user 作答） | 图片轮触发权限卡，用户点继续 | 不新开 round、不覆盖写 live 清单：清单保持上一轮内容，后续成员 harness / 溢出重建仍能物化像素（N10） | `test_control_continuation_keeps_live_round_manifest` |
| slash 命令 | `/evolve` 等命中后早退 | 同样不新开 round，早退路径不写 live 清单（N10） | `test_slash_command_keeps_live_round_manifest` |
| teammate 收到引用行（send_message） | leader 用 send_message 正文保留引用行 | teammate 挂 rail，按"清单 ∩ 自身引用行"物化像素（N10） | — |
| teammate 只拿到路径（任务看板 / 工具结果） | leader 把绝对路径写进 create_task / 任务正文 | 成员 rail 扫到清单图提及后主动补引用行并物化（N10 确定性分发）；不再依赖 leader 消息格式 | `test_mention_in_tool_result_adds_reference_to_user_message` |
| teammate 上下文已 `attached` / 已 `omitted` | 跨模型调用重复进 before_model_call；预算淘汰或读取失败已写 `omitted` | `attached` ∪ `omitted`（含 `omitted: duplicate`）都是终态：不重复补引用行、不重复读盘物化（N5） | `test_already_attached_is_not_repeated`、`test_omitted_image_is_not_rematerialized` |
| 成员 pinned 模型名未声明 | 成员 spec 指定 `not-listed-model`，`models.defaults` 无该条目 | 能力位 False + warning（含成员模型名），**不回落**默认条目的 `modalities`（N2） | `test_unknown_pinned_model_does_not_inherit_default_modalities` |
| 成员只有路径（清单提及未命中，如跨轮清单已清空） | 策略段引导用 read_file 原生读图 | 不引导则惯性 VQA/OCR；read_file 返回图像块后 `native_images_sent>0` 仍会拦 VQA | `test_team_member_path_guidance_points_to_native_read_file` |

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| `modalities` 含 `image` | 原生开启 | 图片没发出去 | `test_sync_keeps_true_for_vision_model` |
| `modalities` 不含 `image` | 解析 False（VQA / 视觉工具链） | 图片塞给纯文本模型导致请求失败 | `test_sync_sets_false_when_model_declares_no_image_support` |
| 条目未声明 `modalities` | 解析 False | 视觉模型被误判为纯文本 | `test_sync_treats_unset_support_as_unsupported` |
| `modalities` 写成字符串 | `coerce_modalities` 归一成列表 | 字符串被逐字符遍历导致判定失真 | `test_coerce_modalities_normalizes_yaml_literals` |
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
| team leader 能力位 | 按成员模型 `modalities` 显式写 bool | 成员 None → SDK 固化 False，rail 挂上也走非原生 | `test_leader_gets_multimodal_rail_and_capability_flag`、`test_member_model_entry_matches_by_model_name` |
| team teammate 也挂 rail | 每个成员都声明 | 无引用行时不会误注入（安全门按清单 ∩ 引用行） | `test_teammate_gets_multimodal_rail_without_dispatch_hint` |
| leader 分发提示 | `dispatch_hint` 只在 leader 生效 | 成员不知道要转发引用行 → 团员仍 0 张 | `test_leader_dispatch_hint_tells_how_to_forward_reference_line` |
| leader 消息只写文件名 | `before_tool_call` 自动补引用行 | 只写任务看板 / 占位符 → 团员看不到像素 | `test_leader_send_message_gets_missing_reference_line` |
| leader 广播 / 回用户 | 不改写 content | 把上传清单发给无关成员或用户 | `test_dispatch_augment_skips_broadcast_and_user_targets` |
| team 非首轮清单刷新 | 写 live build context | 第二轮上传静默 0 张 | `test_bind_round_image_files_writes_live_build_context`、`test_bind_round_image_files_clears_stale_manifest` |
| team 清单到达 rail | provider 读 `request_metadata` | rail 拿不到清单 → 安全门拒全部引用 | `test_provider_binds_round_manifest_from_build_context`、`test_process_team_message_stream_puts_manifest_in_request_metadata` |
| 装配期绑定清单 vs agent 镜像 | 绑定清单优先，空列表不回退 | 读到上一轮残留图 | `test_bound_manifest_wins_over_agent_attribute`、`test_empty_bound_manifest_does_not_fall_back_to_agent_attribute` |
| 清单图已 `omitted`（预算卸载 / 读取失败 / duplicate） | 视为终态：不再补引用行、不再读盘物化 | 每次 LLM 迭代重复补行重编码 → 上下文膨胀、预算抖动 | `test_omitted_image_is_not_rematerialized`、`test_duplicate_omitted_image_is_not_rematerialized` |
| 清单 `1.png` vs 文本 `11.png` / `data.png` | 边界判定不命中（不再裸子串匹配） | 误补无关图、白耗预算 | `test_short_filename_is_not_substring_mention`、`test_longer_manifest_name_is_not_matched_by_shorter_text`、`test_text_mentions_image_requires_token_boundaries`、`test_augment_ignores_substring_filename_mention` |
| 正文路径用反斜杠 / 大小写不同 | 归一后仍命中（`C:\Uploads\3.JPG` 命中 `C:/uploads/3.jpg`） | Windows 形态路径命中失效、只能靠文件名兜底（图被改名即漏判） | `test_path_with_different_separator_and_case_matches`、`test_path_mention_matches_across_separators_without_filename_fallback`、`test_text_mentions_image_normalizes_separator_and_case`、`test_augment_matches_windows_separator_path_mention` |
| 中文紧贴文件名（`请识别3.jpg`） | 命中（边界字符集只含 `[0-9A-Za-z_-]`） | 边界收紧过度导致中文前缀提及漏判 | `test_text_mentions_image_matches_chinese_adjacent_filename` |
| 同一长文本要对多张图判定 | 每条消息只归一一次，再逐图复用 | 逐 (图, 文本) 重复 lower，抵消"全量扫描早退"优化 | `test_normalizes_each_message_text_once_for_all_images`、`test_normalized_text_mentions_image_reuses_one_normalized_text` |
| 控制续写（权限确认后继续）/ slash 命令 | 不覆盖写 live 清单 | 在途图片轮清单被清空 → 后续成员 0 像素 | `test_control_continuation_keeps_live_round_manifest`、`test_slash_command_keeps_live_round_manifest` |
| 普通 follow-up 新轮 | 投递 query 前覆盖写本轮清单 | 绑定位置挪错 → 第二轮上传静默 0 张 | `test_followup_new_round_still_binds_round_manifest` |
| 成员 pinned 模型名未声明 | 能力位 False + warning，不回落默认条目 | 用别的模型能力位决定本成员是否原生看图 | `test_unknown_pinned_model_does_not_inherit_default_modalities` |
| 成员 pinned 用 alias 声明 | 按条目顶层 `alias` 命中同一份 `modalities` | alias 被当成未声明 → 视觉成员退化为工具链 | `test_pinned_model_matches_alias` |
| config 无 `models` 段 / 只有 `models.default` | 前者 False（未声明即不支持），后者按单条目声明解析 | env 兜底条目没有 `modalities` 却参与判定 | `test_config_without_models_section_disables_native_image`、`test_legacy_single_default_entry_is_honored` |
| 能力位解析对模型条目 deepcopy / 解密 | 只读声明字段，不触发 `deepcopy` / `api_key` 解密 | 每成员每轮深拷贝 + 解密纯浪费 | `test_capability_resolution_skips_entry_copy_and_decryption` |

## 修改检查单

- 改能力解析 → 同步 adapter `_resolve_enable_read_image_multimodal`、`model_capabilities`（`coerce_modalities` / `supports_native_image`）、换模同步 `image_capability_sync` 与单测；判定只看 `modalities` 是否含 `image`，禁止恢复内置模型能力表或按模型名前缀猜测
- 改 `deep_config.enable_read_image_multimodal` 的写入点 → N1 单一真源，rail / resolver / stream 护栏 / notice 四处必须同源；不要在 rail 或工具里重新推断模型能力
- 改发送侧决策 → `MultimodalImageRail.before_model_call` 是唯一入口；新增分支必须同时覆盖 enabled / disabled 两种 mutator，且 disabled 只能是视图级（N6）
- 改预算 / 降级顺序 → 保持"最老优先"与状态行可追溯（N5）；0 语义是不限，不能当"禁用"用
- 改已提供图片判定 → 只读 `ctx.extra` 快照（N7）；快照写入点在 `before_invoke`，新增读取方不得依赖 ContextVar
- 改工具护栏 → 同步 [[S_10]] N13/N14 与 `build_provided_image_read_error` / `build_native_image_tool_error`；批量 `file_paths` 必须与单路径同判定
- 改 fallback notice → 只在解析 False 时发（N8）；同时检查系统提示 `multimodal_image_policy` 段与 `ProgressiveToolRail` 的工具披露
- 改 ReadFileTool 构造 → `ConcurrentSafeSysOperationRail.init` 必须同时传 `enable_image_multimodal` 与 `image_multimodal_resolver`；resolver 读 rail 显式值 → `deep_config` → False
- 改上传 / 引用行格式 → `_IMAGE_REFERENCE_LINE_RE` / `parse_image_references` / `build_image_reference_line` 三处同改；旧历史里的旧格式必须仍可解析
- 改 team 原生图片（N10 三要素）→ 能力位（`config_specs` 的 `update["enable_read_image_multimodal"]`）、全成员 rail 挂载（leader 带 `dispatch_hint`）、清单通道（`team_helpers` 的 `request_metadata` 覆盖写 + live build context 刷新）三处必须同时改；只改其一 = 挂 rail 也 0 张。清单键只允许 adapter/team_helpers 覆盖写，禁止信任请求 metadata 里同名外部值
- 改成员授权边界 → 物化前提保持"清单内路径 + 该成员 user-authored 引用行"两条同时成立；任务看板 / 工具结果里的路径不得物化（否则读文件内容可被诱导成像素注入）。想收紧到"按任务分配图"需另建 task→paths 授权表，不能靠放宽安全门实现
- 改成员确定性分发的幂等口径 → "已处理"集合是 `attached` ∪ `omitted`（同消息作用域内 index→path 配对，`omitted: duplicate`、`omitted: compacted` 均为终态）；新增状态行类型必须在此显式决定是否终态，并保留"清单图全部已处理 → 直接返回 0"的早退，否则每轮模型调用都会做全量文本扫描
- 改提及判定（分发兜底 / 成员确定性分发）→ 单源在 `image_reference` 的 `normalize_image_mention_text` / `normalized_text_mentions_image` / `text_mentions_image`：禁止恢复裸 `in` 子串匹配，"分隔符 + 大小写"归一与边界字符集 `[0-9A-Za-z_-]` 是契约；批量场景先按消息归一一次再逐图判定，不要逐 (图, 文本) 重复归一
- 改 leader 分发提示 → 同步 `MultimodalImageRail` 策略段文案与 `dispatch_hint` 装配点；提示变了要重验"leader 用 send_message 转发引用行 → 团员出图"的端到端路径
- 改分发兜底（`before_tool_call`）→ 保持"只补消息真正提到的图（文件名 / 路径）、只对具体成员单播生效、广播 / 回用户不改写"；新增改写必须同步 `augment_message_with_reference_lines` 纯函数测试与 rail 参数改写测试
- 改成员 path-only 引导 → 与 `ReadFileTool` 的 resolver（`ConcurrentSafeSysOperationRail.init` 读 `deep_config.enable_read_image_multimodal`）保持一致；引导文案只在成员 `enabled` 且 0 像素时出现，像素一到位必须切回原生策略段并依靠 N4 拦 VQA/OCR
- 改成员模型能力解析 → 只读**原始 config 的模型条目声明**（`models.defaults` 非空 list 优先、否则 `models.default` 单 dict；都缺 → False），不调 `get_default_models`、不做 `deepcopy` / `api_key` 解密；匹配成员模型名时 `model_client_config.model_name` 与顶层 `alias` 都算命中，**未命中 → False + warning，禁止回落默认条目**（否则拿别的模型能力位决定本成员是否原生看图）；只有未指定模型才取 `is_default` 条目；显式 `base_spec.enable_read_image_multimodal` 优先，不得用"成员一律 True"替代
- 改 team 清单绑定时机（`team_helpers`）→ live build context 只给"确实会开新一轮"的请求写：slash 命令早退之后、投递 query 之前、且 `reuse_active_waiter` 为假；控制续写（adapter 传 `image_files=[]`）与 slash 请求覆盖写会清空在途图片轮清单（N10）

## 与其它 spec 的关系

- [[S_10]]（输入侧守卫）：本 spec 是其 N13/N14 的语义来源；S_10 保留"何时拦截、注入什么文案"的宿主切片，不复制能力解析与预算细节
- [[S_20]]（事件流主通道）：宿主 `JiuSwarmStreamEventRail` 的发射 / sid / cwd 契约归彼处；本 spec 只占用其 `before_tool_call` 的读图分支与 `before_invoke` 的快照缓存点
- [[S_09]]（并发安全）：`ConcurrentSafeSysOperationRail.init` 的 ReadFileTool resolver 装配属本 spec N1/N4 的落地，注册安全仍归 S_09
- [[S_07]]（LLM 调用层失败恢复）：像素预算造成的裁剪不触发溢出恢复；本 spec 的降级发生在发送前，不应与 413 恢复链混用
- `jiuwenswarm/server/runtime/agent_adapter/AGENTS.md`：适配器层"模型缓存 / fallback notice / rail 构建"的散件说明在彼处；本 spec 登记其解析契约（N1/N2/N8），不复制适配器其它铁律
- `jiuwenswarm/common/AGENTS.md`（其 S_11 文件传输 / S_13 请求上下文）：上传件来源与 `_multimodal_image_files` 参数透传归彼处；本 spec 从"引用行已生成"开始
