# B_09 — 图片被上游内容安全拦截（image_url unsafe）整轮失败：无摘图降级重试

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_09 / multimodal-image-unsafe-retry |
| 关联模块 | `jiuwenswarm/agents/harness/common/rails/multimodal_image_rail.py`、`.../rails/multimodal_image_state.py`、`.../rails/stream_event_rail.py`、`.../rails/read_file_validation.py`、`jiuwenswarm/agents/harness/common/multimodal/image_history.py` |
| 修复日期 | 2026-11-24 |
| 关联 spec | [[S_22]]（本修复为 MultimodalImageRail 增补 `on_model_exception` 降级重试并在摘图前建立请求级 unsafe 图片黑名单；S_22 同轮修订至 2026-11-24） |
| 关联测试 | `tests/unit_tests/agents/harness/common/rails/test_multimodal_image_rail_image_unsafe.py`、`.../test_stream_event_rail_image_unsafe_guard.py`、`.../test_read_file_validation.py` |

## 复现场景

OfficeAce 定时任务「每日英语自动生成」2026-11-23 09:05 失败（错误参考号
E-1123-0905-d9eb）。链路（`jiuwenclaw-service-logs/*`）：

1. Agent 按自身沉淀规则「裁出作文图后用 read_file 打开 PNG 核对图像内容」，
   读取裁剪图 `E:\...\imgs\w2006_p28_0.png`（PDF 第 28 页扫描真题作文图，1383x1000）。
2. 图片作为 multimodal `image_url` 输入发送；上游（华为 AgentArts）返回
   `400 - the content of image_url.url field is unsafe`，`gateway.log:5950` 记
   `[MODEL_HTTP] end ... status=error chunks=0 total_ms=2352.4 exc=FrameworkError`。
3. `gateway.log:5951` 记 `[StreamEventRail] Attempting context repair after model
   exception`、`gateway.log:5952` 记 `[MultimodalImageRail] after_model_call
   final_window_images=1`——但没有任何"摘掉图片重试"的动作。
4. `error.2026-11-23.1.log:3` 落 `errorKind=unknown`、`taskDurationMs=312643`，本轮
   直接结束，今日 HTML 未产出。

关键事实：失败调用是本轮**唯一新增内容**（此前 26 次纯文本/工具调用成功），失败 2.35s
即返回、0 chunk，且该错误不属于任何既有恢复链的分类。

## 根因

`MultimodalImageRail` 只有 `before_model_call` / `after_model_call` /
`before_tool_call` 三个 hook，没有 `on_model_exception`。`image_url unsafe` 这错误：

- 不是 413 溢出（`ContextOverflowRecoveryRail` 不接管，见 [[S_07]] N9）；
- 不是 repeat / stream_timeout / transient_invoke（`NotifyingLLMRetryRail` 的三原因
  分流不命中，见 [[S_07]] N2）。

于是三条既有恢复链都不接管，异常直接上抛，图片仍留在历史里，模型 0 chunk 结束、整轮
任务失败且无产物。`StreamEventRail` 那句 "Attempting context repair" 只做工具上下文
修复，不是图片摘除重试。

复审补充：首版修复只有"摘图 + 单次重试"还不够。摘图后 `provided_paths` 变空，
read_file 对同一张图的重复读会被重新放行；本 bug 现场里 agent 的固定习惯正是
"裁图后用 read_file 打开 PNG 核对"，重试后极易再次读同一张图。上游二次 400 时单次
护栏已置位、异常直接上抛，本轮仍 0 chunk 失败——只是多花一次模型调用。因此修复必须
在摘图前把本轮已发出像素的图片路径记为请求级黑名单，并让 read_file 护栏 / 引用行注入
在同一 invoke 内拒绝该路径。

复审补充（CR-2）：read_file 注入的图没有引用行 / 状态行，摘图后只留下路径说明行 +
`[image omitted: unsafe]` 文本。`_processed_image_paths` 现把这两行**相邻配对**也视为
已处理，避免成员侧 `ensure_manifest_images_referenced` 按说明行里的路径重新补引用行、
再次物化；预算卸载的 `[image omitted: request budget]` 同理，防预算淘汰被下一轮
分发绕过。

## 修复口径

在发送侧唯一决策点 `MultimodalImageRail` 上补 `on_model_exception`：

1. **识别**：异常文本同时命中 `image_url` 与 `unsafe`（`_is_image_unsafe_error`），
   对应上游 `the content of image_url.url field is unsafe`；不命中直接返回（不误伤）。
2. **摘图**：新增 `image_history.strip_image_content_from_messages(messages, reason="unsafe")`，
   就地摘除历史里全部 `image_url` 块，并把 `[image #n attached]` 状态行改写为
   `[image #n omitted: unsafe]`（终态，[[S_22]] N5 语义）；read_file 注入、无状态行的
   像素块换成 `[image omitted: unsafe]` 文本并保留其路径说明行。
3. **重试一次**：`ctx.request_retry(0)`。agent-core 的 `@rail` 重试循环在
   `request_retry` 后**重新 fire `before_model_call` 并重跑窗口构建**——重试时注入因
   `omitted` 状态行跳过同一路径、窗口从已摘图历史重建为纯文本，模型拿到文本继续任务。
4. **请求级黑名单**：摘图前用 `collect_image_content_paths` 收集本轮**已发出像素的
   全部图片路径**（引用行 `attached` 图 + read_file 图），写入
   `mark_unsafe_image_paths`（`ctx.extra["_jiuwenswarm_unsafe_image_paths"]` + agent
   属性）。摘图后 `provided_paths` 变为空，read_file 会重新放行同一张图；若它再次进入
   窗口，上游二次 400 时单次护栏已置位、异常直接上抛，本轮仍 0 chunk 失败。黑名单让
   `StreamEventRail.before_tool_call` 拒绝这些路径（`build_unsafe_image_read_error`），
   并让 `inject_image_content_from_references(blocked_paths=...)` 跳过引用行重新物化
   （写 `omitted: unsafe` 终态）。生命周期与护栏绑定：新 invoke 首次
   `before_model_call`（guard 未置位）清空，降级重试重跑时保留。
5. **单次护栏**：`ctx.extra["_multimodal_image_unsafe_stripped"]` 置位后再次命中不再
   重试，异常交上层（避免无限重试）。

效果：单张图片被上游内容安全拦截时，本轮自动"去图、纯文本重发一次"，保住此前的工具
结果与后续生成步骤；重试后模型即便再次 read_file 同一张图，也会收到
`[READ_FILE_ERROR]` 提示而不是把 unsafe 图塞回窗口；重试仍失败（或换成另一张同样被
拦截的新图）才按原样失败。

## 验证

- 新增定向测试（`test_multimodal_image_rail_image_unsafe.py`）：摘图 + 状态行改写 +
  单次重试 + 非 unsafe 不误伤 + 摘图前黑名单收录两类路径 + 新请求清空 / 降级重试保留 +
  引用行注入跳过黑名单路径。
- 新增工具护栏测试（`test_stream_event_rail_image_unsafe_guard.py`）：黑名单路径
  read_file 被拒、其它未提供图片仍放行、无 ctx 时可从 agent 属性读黑名单。
- `test_read_file_validation.py` 增补 `build_unsafe_image_read_error` 中英文文案断言。
- CR-2 增补（`test_manifest_mention_injection.py`）：read_file 图的「路径说明行 +
  `[image omitted: unsafe]` / `[image omitted: request budget]`」相邻对被
  `_processed_image_paths` 视为已处理，清单门不再补引用行。
- 回归：`tests/unit_tests/agents/harness/common/rails/` + `.../multimodal/` 共
  **274 passed**；`test_stream_event_rail_{tool_result,todo,symphony,source_skill}.py`
  共 **29 passed**。
- 未覆盖（真实网关不可用）：端到端 400 往返后的自动降级重试与二次 read_file 拦截。

## 遗留

- **摘图粒度是全窗口所有图片**：上游不返回"具体哪张 unsafe"，本地只能把本轮窗口图片
  整体降级为纯文本；同轮多图且仅一张被误判时，其余图也会一并文本化（保守行为）。
- **重试一次后仍失败直接上抛**：同一张已判 unsafe 的图经 read_file / 引用行重新进入
  窗口的路径已由请求级黑名单封住；但重试后若读入**另一张**同样被上游拦截的新图，仍按
  "只降级一次"上抛，不做自动换模型 / 转 OCR（那是模型/上层决策）。
- **错误分类需 relay-claw 配合映射 code**：本仓 server 侧已让 `chat.error` 帧对
  `image_url unsafe` 错误带结构化 `code=image_url_unsafe`（`stream_utils.build_chat_error_event`
  兜底识别，不再依赖字符串匹配）；relay-claw 仍需在 `classifyError` 认识并映射该
  code 才能落到友好文案，否则仍显示 unknown / OA.05000090。