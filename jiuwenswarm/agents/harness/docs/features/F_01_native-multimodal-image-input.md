# F_01 原生多模态图片输入

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature |
| 编号 / slug | F_01 / native-multimodal-image-input |
| 归档日期 | 2026-10-31 |
| 关联 spec | S_22_native-multimodal-image-input.md（新增）；S_09 / S_10 同步修订 |
| 关联提交 | `fix: 支持原生多模态能力`（2026-10-31，HEAD 2ede14e02）+ 本次收口修正 |

## 背景与目标

此前图片只能走"视觉工具链"：用户上传图片 → 消息里写引用行 → 模型必须调用
`visual_question_answering` / `image_reading` 读图。对支持原生看图的模型（GPT-4o /
Claude / Gemini / Qwen-VL 等）这是多余的一跳：多一次工具往返、多一次 base64 传输，
且模型拿不到图片的原始视觉上下文。

本次改动让**支持看图的模型直接收到像素**：上传件在发送前物化成多模态 content，模型用
自身视觉能力回答；不支持看图的模型保持原工具链。目标：

1. 模型能力决定链路，用户不需要理解"哪种模型该怎么配"；
2. 同一份历史在模型切换后可复用（视觉模型看到像素、纯文本模型看到文字状态行）；
3. 上下文膨胀可控（字节 / token / 张数三预算 + 最老优先降级）；
4. 工具侧不再重复读图、不再误调视觉工具。

## 关键决策

| 决策 | 选择 | 被拒绝的方案 / 理由 |
|---|---|---|
| 是否原生看图的判定位置 | adapter 每请求解析一次，写 `deep_config.enable_read_image_multimodal`，其余组件只读 | 各 rail / 工具各自推断 → 必然漂移（已出现过 read_file 用 init 时旧值的问题） |
| 模型条目与显式配置的优先级 | 条目声明不支持 = 硬边界，显式 true 不能覆盖；条目声明支持时显式 false 仍可关 | "显式配置永远最高" → 用户把 `enable_read_image_multimodal: true` 留在纯文本模型上，请求必然失败；"条目永远最高" → 用户无法主动降级 VQA |
| 像素写历史还是只写视图 | 视觉模型：像素写进历史（后续轮次复用、不重复读文件）；非视觉模型：只剥发送视图 | 全部只写视图 → 每轮重复读文件 + base64 重建；全部写历史 → 纯文本模型历史里堆 base64，切模型后无法清洗 |
| 非视觉模型的剥离方式 | `model_copy` 视图级过滤，历史 base64 保留 | 就地清洗历史（原 `strip_image_content_from_model_context`）→ 切回视觉模型后图片永久丢失，且污染 checkpointer |
| 已提供图片的判定 | `before_invoke` 写 `ctx.extra` 快照，工具侧读快照 | 只读 ContextVar → 工具跨任务 / 线程执行时丢失，护栏失效，重复读图 |
| 重复读已提供图片 | 整批拒绝（含 `file_paths` 任一项） | 放行 → 同一张图两份 base64；只拦单路径 → 批量读绕过 |
| 原生模式视觉工具 | 拒绝并提示"已提供像素" | 放行 → 模型多绕一圈工具，还可能拿到与像素不一致的二次描述 |
| 预算超限策略 | 最老优先降级为 `omitted` 状态行 | 新图优先 → 用户刚发的图被丢，体验更差；整请求失败 → 一张大图毁掉整个问题 |
| 提示文案 | 仅解析 False 时发"已切换为工具" | 解析 True 也发 → 与"像素已发送"自相矛盾（本次修正点） |

## 改动清单

### 新增

- `jiuwenswarm/agents/harness/common/rails/multimodal_image_rail.py`：`MultimodalImageRail`（priority 75），发送侧唯一决策点，安装物化 / 过滤 mutator + 维护系统提示 `multimodal_image_policy` 段。
- `jiuwenswarm/agents/harness/common/tools/model_capabilities.py`：三态能力解析（`resolve_model_entry_support` / `resolve_model_supports_image` / `builtin_model_supports_image`）。
- `user_prompt_builder` 图片函数族：引用行生成 / 反解、像素注入与预算、视图过滤、视觉 token 估算、路径归一、ContextVar 图片列表。
- `read_file_validation` 新增 `build_provided_image_read_error` / `build_native_image_tool_error` / `extract_paths_from_arguments`。
- 测试：`tests/unit_tests/agents/harness/common/tools/test_model_capabilities.py`、`.../rails/test_multimodal_image_rail_view.py`、`.../rails/test_native_image_read_guard.py`、`.../rails/test_concurrent_safe_rails.py`、`tests/unit_tests/server/runtime/agent_adapter/test_model_capability_resolution.py`、`test_image_tool_fallback.py` 等。

### 修改

- `interface_deep.py`：`_resolve_enable_read_image_multimodal` 三态解析；`_build_multimodal_image_rail` 装配；`_build_image_tool_fallback_notice` 与解析结果对齐；删除 no-op `_prepare_react_image_tool_prompt` 及其三个调用点；`_multimodal_image_files` 参数归一。
- `interface_code.py` / `interface_flash.py`：rail 构建与预算传参、fixed rail 名单。
- `stream_event_rail.py`：`before_invoke` 缓存图片路径快照；`before_tool_call` 读图 / 视觉工具护栏三分支。
- `concurrent_safe_rails.py`：`ReadFileTool` 同时收 `enable_image_multimodal` 与 `image_multimodal_resolver`，resolver 调用时求值。
- `progressive_tool_rail.py`：原生模式下视觉工具渐进披露策略。
- `resources/config.yaml`：三预算配置与三态注释。

### 本次收口修正（相对 2ede14e02）

1. 模型条目声明为硬边界：`supports_image=False` / `modalities` 无 image 时显式 true 不再生效；`_resolve_enable_read_image_multimodal` 与 `config.yaml` 注释同步。
2. 内置能力表窄化：移除宽前缀 `o1`；`gemini-` / `llama-3.2` / `mistral-small-3` 收窄到具体视觉变体；新增 text-only 前缀表（`o3-mini` / `o3-pro`）并优先判定。
3. `ConcurrentSafeSysOperationRail.init` 补 `image_multimodal_resolver`，修复"切模型后 read_file 仍按旧模型行为"。
4. 已提供图片判定改用 `ctx.extra` 快照，修复工具跨任务 / 线程时 ContextVar 丢失导致的护栏失效；新增批量 `file_paths` 判定与原生模式视觉工具拒绝。
5. 删除死代码：`strip_image_content_from_model_context`、`extract_vision_target`、`_prepare_react_image_tool_prompt`；文档同步（本 F_01 + S_22 + S_09/S_10 修订 + AGENTS 索引）。
6. 提示一致性：解析 True 时 `_build_image_tool_fallback_notice` 返回 None，消除"像素已发送但仍提示已切换工具"的矛盾。

## 实现偏差与取舍

- **条目硬边界是新增语义**：2ede14e02 的行为是"显式 true 无条件生效"。本次改为条目优先，理由是请求必然失败比"用户配置被忽略"更糟；代价是显式 true 在纯文本条目上静默失效。文档与注释已明示这一非对称性。
- **`is_current_multimodal_image_path` 保留**：作为同调用栈便捷包装（内部委托 `is_provided_image_path`），避免一次性改所有 prompt 侧调用点；跨任务场景已明确要求走 `ctx.extra` 快照。
- **rail 构造失败降级 None**：装配失败不阻断 agent 创建（与其它 rail 一致），代价是该请求退回"无 rail"（图片按纯文本引用行发送，模型看不到像素）。
- **视觉 token 估算为近似值**：512px tile 计费（85 + 170×tiles），与厂商真实计费可能有偏差；预算默认 0（不限），只在用户显式配置时生效。

## 验证

2026-10-31 本地单测（`.venv\Scripts\python.exe -m pytest ... -o addopts="--asyncio-mode=auto" -p no:cacheprovider -q`，
日志 / 数据目录经 `LOG_ROOT_PATH` / `JIUWENSWARM_DATA_DIR` / `JIUWENSWARM_CONFIG_DIR` 重定向到仓内临时目录）：

- 全量合并跑（harness `common/rails/` + `common/tools/test_model_capabilities.py` + `common/prompt/` +
  `server/runtime/agent_adapter/test_model_capability_resolution.py` + `test_image_tool_fallback.py` +
  `test_prepare_image_inputs.py` + `agentserver/test_progressive_vision_tool_guard.py` +
  `test_flash_adapter_rails.py` + `test_deep_adapter_interrupt.py` +
  `test_stream_event_rail_tool_result.py` + `test_stream_event_rail_symphony.py`）：
  **292 passed, 1 skipped**（skip 属既有 live 环境用例），pytest 退出码 0。
- 覆盖点：能力三态解析与条目硬边界、内置窄表（含 text-only 变体）、rail enabled/disabled 两种
  mutator、视图剥离不改历史、三预算最老优先降级、`ctx.extra` 快照跨 ContextVar reset、
  批量读已提供图片拒绝 / 未提供图片放行 / 非图片二进制拒绝、原生模式视觉工具拒绝与非原生放行、
  ReadFileTool resolver 跟随 `deep_config` 切换、fallback notice 一致性、deep adapter 中断路径回归。

> 说明：本机沙箱曾对 `tmp_path` 与 `pytest-of-*` 基础目录报 `PermissionError`，相关用例在
> 放开文件权限后复跑通过；该环境问题与代码无关，未在仓内落任何绕过逻辑。

## 遗留 / Follow-up

1. 图片 base64 会随历史落盘（checkpointer / history.json），体积与隐私影响需在存储侧评估；当前只在预算层做约束，未做"历史里只留引用行 + 每次重读"的替代方案。
2. `_build_image_tool_fallback_notice` 的中英文文案与前端提示的对应关系未在系统测试覆盖。
3. 视觉 token 估算是近似值，未按厂商真实 tokenizer 校准。
4. `ProgressiveToolRail` 的视觉工具披露策略只做了原生 / 非原生二分，未覆盖"原生模型但图片读取失败降级"的中间态。
