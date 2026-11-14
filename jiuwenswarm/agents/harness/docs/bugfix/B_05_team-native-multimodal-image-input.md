# B_05 — team 模式原生多模态未生效：leader 没挂 rail、能力位默认 False、上传清单到不了成员

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_05 / team-native-multimodal-image-input |
| 关联模块 | `jiuwenswarm/agents/swarm/config_specs.py`、`jiuwenswarm/agents/swarm/providers/multimodal.py`（新增）、`jiuwenswarm/agents/swarm/registry.py`、`jiuwenswarm/agents/harness/common/rails/multimodal_image_rail.py`、`jiuwenswarm/agents/harness/common/multimodal/image_reference.py`、`jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`、`jiuwenswarm/server/runtime/agent_adapter/team_helpers.py` |
| 修复日期 | 2026-11-05 |
| 复查修复日期 | 2026-11-06（PR !9634 检视意见，见「复查修复」一节；S_22 同轮修订至 2026-11-06） |
| 关联 spec | [[S_22]]（本修复为该 spec 增补 N10 / P9 / team 契约；S_22 修订日期 2026-11-05，复查后为 2026-11-06） |
| 关联测试 | `tests/agents/swarm/test_team_multimodal_image.py`、`tests/unit_tests/agents/harness/common/multimodal/test_team_image_manifest.py`、`tests/unit_tests/agents/harness/common/multimodal/test_manifest_mention_injection.py`（复查修复）、`tests/unit_tests/agents/harness/common/multimodal/test_image_reference.py`（复查修复）、`tests/unit_tests/agents/harness/common/rails/test_multimodal_image_rail_bound_files.py`、`tests/unit_tests/agentserver/test_team_multimodal_round_manifest.py`、`tests/unit_tests/agentserver/test_evolve_command_edges.py`（team 图片引用投影） |

## 复现场景

现场会话 `web_1a1aab89656_75803007d563`（`mode=team`，团队
`image-recognition-squad_2_...`）里，用户上传 `0.jpg` / `1.jpg` 并要求识别。响应表面正常
（模型描述了幼兔、水印、昵图网 ID），但 trace 显示识别全部来自
`visual_question_answering` 工具，**原生图片输入从未发生**：

- 该会话 trace（`~/.jiuwenswarm/.trace/sessions/...sqlite3`，trace id
  `9a1546c5c411d6d427091ff786c5510f`）里 9 个 `context.window.commit` 窗口（leader 4 /
  vision-analyst 5）**image 块全部为 0**；leader 的 user 消息只有
  `[image #1] path=...` 文本引用行；
- 系统提示里没有 `multimodal_image_policy` 段、运行时附件里没有
  `runtime.image_state`，即 `MultimodalImageRail.before_model_call` 在成员 agent 上从未运行；
- 对照：同机 166 个会话 trace 中，非 team 的 164 个里有 54 个出现 image 块（单 agent
  正常），2 个 team 会话全部为 0。

**跟进复核 1（2026-11-05，`web_1a1ab96b56d_9a035826e524`，leader-only 版本）**：leader
侧已正常（9 个窗口 image 块=2、`attached` 行与"本次请求附带图片：2 张"附件齐全，leader 未再
调用 VQA/OCR，直接凭像素写出 2.jpg 描述）；但同队 `image-recognizer` 10 个窗口全部
image 块=0、无引用行，仍靠 `visual_question_answering`/`image_ocr`/PIL 裁剪，并再派
general-purpose 子代理重复读图（30 次 LLM 调用）。据此把 rail 扩到全成员并补 leader
分发提示（见「修复口径」第 2、4 条）。

**跟进复核 2（2026-11-05，`web_1a1abb54384_ffb778318a1a`，全成员 rail + dispatch_hint 版本）**：
leader 仍正常（8 个窗口 image 块≥2；`create_task` 正文已带完整
`[image #2] path=...` 引用行）；但 teammate（`vision-analyst`）13 个窗口 image 块全 0。
直接原因：引用行只落在 **`create_task` 任务正文**（teammate 通过 `view_task` 的 tool result
读取，不物化），而 leader 的 `send_message` 正文只有 `[image #...] path=...` **占位符**，
teammate 的 user 消息里没有任何可解析引用行 → 团员继续 VQA/OCR。
注：该 trace 的 system message 长度恒为 10266，无法从 trace 证明系统提示里有没有分发提示，
只能确认"最终 user 消息里没有引用行"这一事实。据此增加 `before_tool_call` 确定性兜底
（`send_message` 正文自动补引用行）与成员侧确定性分发（`before_model_call` 扫上下文提到
的清单图、补引用行后物化），使"模型声明原生 + 图在本轮清单内"不再依赖 leader 消息格式。

## 根因

team 成员的装配链与单 agent 不同，原生图片的三要素在这条链上**三处同时断裂**：

1. **rail 没挂**：单 agent 的 rails 在 `interface_deep._build_agent_rails` 里含
   `_multimodal_image_rail`；team 成员的 rails 由 `agents/swarm/config_specs.py` 声明式装配
   （`_COMMON_RAIL_NAMES` / `_build_team_capability_specs`），其中没有 multimodal rail，
   swarm provider 里也没有对应实现。
2. **能力位默认 False**：openjiuwen `agent_configurator` 对成员
   `enable_read_image_multimodal is None` 一律兜底 False（省掉每成员一次探测 LLM 往返），
   而 `build_member_deep_agent_spec` 从不写该字段——即便 rail 挂上也会走非原生分支。
3. **上传清单到不了成员**：adapter team 分支只把清单镜像到 `self._instance`（adapter 自己的
   DeepAgent）并 set 请求级 ContextVar；team leader 是 Runner 另建的成员 agent，两者都拿不到，
   而 `inject_image_content_from_references` 的安全门要求"路径必须在本次上传清单内"，
   于是所有引用行被当作 non-uploaded 跳过（静默 0 张）。

## 修复口径

三要素按 N10 成对补齐，只补其一仍会 0 张：

1. **能力位（声明式，不探测）**：`config_specs._resolve_member_native_image_enabled` 按成员
   模型条目（成员 spec 指定模型则按名匹配 `models.defaults`，否则回退默认条目）的
   `modalities` 解析，`build_member_deep_agent_spec` 显式写
   `update["enable_read_image_multimodal"]`；blueprint 已显式写该字段时以显式值优先。
   （复查修复后：指定模型名未命中条目 → False + warning、不回落默认条目，见「复查修复」
   Nice 条。）
2. **rail（全成员）**：新增 `swarm.multimodal_image` provider
   （`agents/swarm/providers/multimodal.py`，已进 registry 与 catalog）；`build_member_deep_agent_spec`
   为**每个成员**（leader / teammate / 命名成员）声明该 RailSpec，params 携带能力位与
   `react.multimodal_image_*` 三预算。leader 额外带 `dispatch_hint=True`（见第 4 条）。
   授权边界不在挂载处而在物化处：成员只会物化"本轮上传清单内 + 自己历史里 user-authored
   引用行"的图；任务看板 / 工具结果里的纯路径不物化。
3. **清单（build context 通道）**：`MultimodalImageRail.__init__` 新增可选 `image_files`
   与 `_resolve_request_image_files()`（绑定清单优先、空列表不回退 agent 属性、未绑定时
   保持单 agent 旧行为）；`image_reference` 新增
   `store_team_request_image_files` / `team_request_image_files`（覆盖写 / 读
   `request_metadata["multimodal_image_files"]`，读侧返回副本）；`interface_deep` team
   分支把 `image_files` 传入 `process_team_message_stream`；`team_helpers` 在 enrich 前
   覆盖写 request_metadata（首轮 / 冷恢复随 fresh spec 生效），并把同一清单写进 live team
   agent 的 `harness.build_context.request_metadata`（跨轮复用 runtime 每轮据此重建
   NativeHarness，非首轮上传才能生效）。进程内 teammate 与 leader 共享同一 dict，因此
   同轮内 spawn 的成员能读到本轮清单；子进程 / 分布式成员取 spawn 时 seed 快照（见遗留）。
4. **leader 分发提示 + 确定性兜底**：leader 的 `multimodal_image_policy` 段在原生
   有图时追加规则："要把图交给成员，请用 `send_message` 正文原样保留
   `[image #n] path=... filename=... mime=...` 引用行；只给路径或只写任务看板，成员看不到
   像素。"——成员侧 rail 的物化入口仍是 user-authored 引用行，任务的工具结果通道不触发。
   仅有提示不够（实测 leader 会把引用行只写进 `create_task`，消息里只留 `[image #...]`
   占位符）：leader rail 的 `before_tool_call` 对 `send_message` 做兜底——正文提到本轮
   上传图的文件名 / 完整路径时，自动在正文末尾补对应引用行（广播 `to="*"` / 回用户
   `to="user"` 不改写，单条最多 4 张）。
5. **成员侧确定性分发（不依赖 leader 消息格式）**：成员 rail 在 `before_model_call`
   扫描自身上下文（含任务看板 / `view_task` 等 tool result 文本，仅用于判断"提到了哪张
   图"），命中本轮上传清单的文件名 / 完整路径、且该图尚未 `attached` 时，主动把引用行
   追加到最近一条 user 消息，再走既有 inject 物化。效果：**成员模型声明原生能力 + 图在本轮
   上传清单内**时，即使 leader 只把路径写进任务看板、消息里只有占位符，成员也能原生看图；
   像素一旦提供，既有护栏按 S_22 N4 拒绝同一图的 VQA/OCR。幂等：已 attached 不重复补
   （复查修复后「已处理」扩为 attached ∪ omitted，见「复查修复」CR-9634-1）。
6. **成员 read_file 原生读图 + 引导**：原生开启时 `ReadFileTool` 的 resolver 每次调用读
   `deep_config.enable_read_image_multimodal`（该值已由第 1 条按成员模型写入），成员对本地
   图片路径 `read_file` 会拿到图像内容而不是文本；成员 rail 在 `enabled` 且本轮 0 像素时写
   `multimodal_image_policy` 引导"本地路径优先 read_file，不要 VQA/OCR"。read_file 返回的
   图片块会计入下一次 `native_images_sent`，VQA/OCR 继续被拦。这条路径弥补"清单提及未命中
   （如跨轮清单已刷新为空）"的场景。

## 验证

- 环境说明：本工作区未建 `.venv`（缺 openjiuwen / pyyaml 等），回归使用本机已装的
  参考 Python 环境（openjiuwen 0.1.16）覆盖 rootdir 为本仓运行；目标 SDK 由 pyproject
  指到 agent-core `dev-stable`，升级后需按 S_22 检查单复验（尤其
  `agent_configurator` 的 None 兜底与 `TeamHarness` 每轮重建语义）。
- 新增定向测试 **31 passed**：
  `tests/agents/swarm/test_team_multimodal_image.py`（11）、
  `tests/unit_tests/agents/harness/common/multimodal/test_team_image_manifest.py`（7）、
  `tests/unit_tests/agents/harness/common/rails/test_multimodal_image_rail_bound_files.py`（6）、
  `tests/unit_tests/agentserver/test_team_multimodal_round_manifest.py`（7）。
- 回归：`tests/agents/swarm` + `common/multimodal` + `common/rails` 共 **401 passed**；
  `tests/unit_tests/agentserver/test_team_helpers.py` +
  `test_deep_adapter_interrupt.py` 共 **175 passed / 1 skipped**；
  `test_evolve_command_edges.py` **22 passed**（team 图片引用 + 新增 `image_files` 传参断言）；
  `tests/unit_tests/agents/harness` 全量 **435 passed / 2 failed**，失败两例为
  `test_image_tools_imports.py` 的可选依赖探测（参考环境缺 `google.genai`），与本修复无关；
  新增 + 改动测试复跑 **72 passed**；其中分发兜底新增用例 9 条
  （`test_image_reference.py` 6 + `test_multimodal_image_rail_bound_files.py` 3）、
  成员侧确定性分发新增 6 条（`test_manifest_mention_injection.py`）、
  成员 read_file 引导新增 3 条（`test_multimodal_image_rail_bound_files.py`）；
  全量相关回归 **623 passed / 1 skipped**。
- 未覆盖（本机无完整依赖 / 无真实模型）：端到端 team 会话真实出图；fallback notice 口径；
  分布式 teammate 的辅助 leader（`team_manager.create_team` 的 `TeamBootstrapParams` 路径
  不带图片清单）。

## 复查修复（PR !9634 检视意见）

首轮修复合入后收到检视意见：Should Fix 3 条（`renquanjia` 的 CR-9634-1 / CR-9634-2、
`KevinLLLove` 的 CR-9634-6）+ Nice 2 条（能力位解析）。以下按条目记录复现、根因、修复
口径与回归用例。上述改动只调整既有链路的判定与时机，**不改变**本文前面记录的
三要素装配、rail 挂载、清单通道与授权边界事实（S_22 修订日期同步为 2026-11-06）。

### CR-9634-1（Should Fix）已 `omitted` 的清单图每轮重新补引用行 + 重新物化

- **复现场景**：成员上下文里某张清单图已被预算淘汰 / 读取失败 / 判重，历史中留下
  `[image #1] path=...` 引用行 + `[image #1 omitted: request budget]` 状态行（或
  `omitted: duplicate`），而任务看板 / tool result 里仍提到该路径。下一次 LLM 迭代的
  `before_model_call` 里，成员确定性分发又补一条 `[image #2]` 引用行，
  `inject_image_content_from_references` 再读盘、缩放、编码，再写一条 `omitted`——
  每个模型迭代重复一次，上下文无界膨胀，预算卸载过的图被反复重读重编码。
- **根因**：幂等集合由 `_attached_image_paths` 生成，只收 `attached` 状态行对应的路径；
  预算淘汰 / 读取失败写下的 `omitted` 图（及其 `duplicate` 变体）不在集合里，因此每次
  都被当成"还没给过"。
- **修复口径**：`_attached_image_paths` → `_processed_image_paths`，`attached` ∪ `omitted`
  （含 `omitted: duplicate`）都是终态，不再补引用行、不再物化。集合按**消息作用域**配对：
  同一条 user 消息内用 `_message_image_states` 的状态行 index 去取同消息
  `parse_image_references` 的 index→path 映射，避免压缩把老消息搬进摘要后、新消息里的
  同号引用被老状态行误判成"已处理"；引用行仍只认 user-authored 消息（tool result /
  assistant 文本里的同格式行不得参与状态配对，否则文件内容可被伪造成"已处理"）。
- **附带优化（Nice）**：`ensure_manifest_images_referenced` 先把清单按已处理集合过滤，
  过滤后为空直接返回 0——本函数每次模型调用都跑，"清单图全部已处理"是稳定状态下的
  常态，不必再对全部消息做归一扫描。
- **回归用例**：`test_omitted_image_is_not_rematerialized`（含 `inject_image_content_from_references`
  断言 attached==0 且引用行未变）、`test_duplicate_omitted_image_is_not_rematerialized`。

### CR-9634-2（Should Fix）文件名裸子串匹配过宽 / Windows 路径形态命中失效

- **复现场景**：清单里是 `1.png`，tool result 出现 `11.png` 或 `data.png` → 裸 `in` 命中，
  误补引用行并把无关图物化；反方向清单 `11.png` 也会被文本里的 `1.png` 命中。另一面：
  清单路径 `C:/uploads/3.jpg`，正文写成 `C:\Uploads\3.JPG` 时路径比较失效（分隔符 /
  大小写不一致），只能靠文件名兜底——图被改名（`filename` 与路径 basename 不同）即整体
  漏判。leader 分发兜底 `augment_message_with_reference_lines` 有同类过宽匹配。
- **根因**：`hit = any(path_key in text or (filename and filename in text) ...)` 是裸子串
  判定，既不认边界，也不做分隔符 / 大小写归一；`augment_message_with_reference_lines`
  的 `path not in content and filename.lower() not in lower` 同理。
- **修复口径**：在 `image_reference.py` 增加共享纯函数，两个分发入口共用同一判定
  （`image_reference` 不 import `image_history`，依赖方向保持单向）：
  - `normalize_image_mention_text(text)`：只做 `\` → `/` 与 lower。**不用**
    `normalize_image_path`——realpath 会改写正文里的相对段 / `~`，且 POSIX 的
    `os.path.normcase` 恒等，对齐不了 Windows 反斜杠形态；
  - `normalized_text_mentions_image(image, normalized_text)`：在**已归一**文本上按
    "完整路径 / basename" 做边界正则（`(?<![0-9A-Za-z_\-])...(?![0-9A-Za-z_\-])`），
    供批量场景先归一一次再逐图复用；
  - `text_mentions_image(image, text)`：单条文本的便捷封装（内部归一一次）。
  三个函数经 `multimodal/__init__.py` 导出。`ensure_manifest_images_referenced` 用前两个
  （每条消息归一一次，再对多张清单图复用）；`augment_message_with_reference_lines` 改用
  `text_mentions_image`。已 attached/omitted 幂等、`max_images` 限张、序号分配、广播 /
  user 不改写等既有行为保持不变。效果：`请识别3.jpg` 仍命中（边界字符集只含 ASCII 文件名
  类字符），`1.png` 不再命中 `11.png` / `data.png`，`C:\Uploads\3.JPG` 能命中
  `C:/uploads/3.jpg`。
- **回归用例**：`test_manifest_mention_injection.py` 的
  `test_short_filename_is_not_substring_mention`、
  `test_longer_manifest_name_is_not_matched_by_shorter_text`、
  `test_path_with_different_separator_and_case_matches`、
  `test_path_mention_matches_across_separators_without_filename_fallback`、
  `test_normalizes_each_message_text_once_for_all_images`（归一调用次数 = 消息数）；
  `test_image_reference.py` 的 `test_text_mentions_image_requires_token_boundaries`、
  `test_text_mentions_image_matches_chinese_adjacent_filename`、
  `test_text_mentions_image_normalizes_separator_and_case`、
  `test_augment_ignores_substring_filename_mention`、
  `test_normalized_text_mentions_image_reuses_one_normalized_text`、
  `test_augment_matches_windows_separator_path_mention`。

### CR-9634-6（Should Fix）控制续写清空 in-flight 图片轮的 live 上传清单

- **复现场景**："上传图片 → 成员触发权限确认 / ask_user → 用户同意继续"这一轮。控制续写
  请求由 adapter 传 `image_files=[]`（只做作答投递、不开新轮），旧实现在 slash 处理之前
  无条件 `await _bind_round_image_files(team_name, round_image_files)`，把 live team agent
  的 `harness.build_context.request_metadata["multimodal_image_files"]` 覆盖写为空；随后
  流程在 `reuse_active_waiter` 分支只把作答投递给既有 waiter 并 return。该图片轮后续新建
  的成员 harness（或上下文溢出恢复重建）读到空清单 →
  `ensure_manifest_images_referenced` 不再补引用行、`inject_image_content_from_references`
  把 `[image #n]` 判为"不在本轮清单内"丢弃，原生图片输入失效。slash 命令命中后的多条早退
  `return` 同理会清空。
- **根因**：清单绑定位置在任何"这个请求到底会不会开新一轮"的判定之前，且对
  `reuse_active_waiter` 没有守卫。
- **修复口径**：绑定收敛到"确实会开新一轮"的请求——移到 slash 命令处理块**之后**、投递
  query（`team_manager.interact` / `_start_team_stream_round`）**之前**，并加
  `if not reuse_active_waiter` 守卫；注释写明"为什么控制续写 / slash 不写、为什么必须在
  投递前写"（`TeamHarness` 每轮按同一 build context 重建 NativeHarness，投递后再写赶不上
  本轮成员的 rail 构造）。首轮 / 冷恢复（`_prepare_first_team_request` 路径的
  `request_metadata` 覆盖写）与普通 follow-up 新轮仍照常绑定；`_bind_round_image_files` /
  `_bind_round_image_files_to_agent` 自身的"覆盖写 / 空列表清除"语义不变（首轮 / 冷恢复的
  fresh spec 清理依赖它）。失败早退路径（cancel 拆除在途 / gate_closed / interact 失败）
  仍会先写入清单，与修复前一致：这些请求不新开 round，清单不会被该轮读取，下一轮投递前
  必再覆盖。
- **回归用例**：`test_control_continuation_keeps_live_round_manifest`（复现原故障：live
  清单原样保留、流向 `chat.processing_status_deferred` 后 return）、
  `test_slash_command_keeps_live_round_manifest`（slash 早退不清空）、
  `test_followup_new_round_still_binds_round_manifest`（普通新轮仍在投递前绑定）。

### Nice：成员 pinned 模型名未命中不再回落默认条目

- **复现场景**：`models.defaults` 只有 `vision-model`（`modalities` 含 `image`），成员 spec
  却 pinned 了一个未声明的模型名（拼错 / 已下线 / 只在别处声明）。旧实现按名循环未命中后
  落到默认条目，用 `vision-model` 的能力位判 True——纯文本成员被当成视觉成员（像素塞给不
  支持模型），反向也会把视觉成员误判成纯文本。
- **根因**：N2「模型条目是唯一依据、未声明即不支持」在"指定了模型名但查不到条目"这条分支
  上没有落实，静默回落到"别的模型"的声明。
- **修复口径**：成员模型名按条目 `model_client_config.model_name` **或**顶层 `alias` 匹配
  （成员 spec 可能用 alias 声明）；两者都未命中 → `logger.warning`（带成员模型名，便于
  定位配置漂移）+ 返回 False，**不回落默认条目**。只有成员未指定模型时才取
  `is_default is True` 的条目（否则第一个）。显式
  `base_spec.enable_read_image_multimodal` 仍优先。
- **回归用例**：`test_unknown_pinned_model_does_not_inherit_default_modalities`（含 warning
  文案断言）、`test_pinned_model_matches_alias`。

### Nice：能力位解析不再对模型条目 deepcopy / 解密 api_key

- **复现场景**：team 装配按「每个成员、每一轮」调用
  `_resolve_member_native_image_enabled`，旧实现每次都调 `get_default_models(config)`——
  内部对全量模型条目 `copy.deepcopy` 并逐个解密 `api_key`，而本判定实际只需要
  `model_client_config.model_name` / `alias` / `modalities` 三个声明字段。
- **修复口径**：新增 `_declared_model_entries(config)` 直接读原始 config，选取优先级与
  `get_default_models` 一致（`models.defaults` 非空 list 优先，否则 `models.default` 单
  dict 包装；两者都没有 = env 兜底形态 → 空列表 → 能力位 False，与"未声明即不支持"一致，
  旧 env 兜底条目本来也没有 `modalities`）；移除 `get_default_models` import。`_model_entry_name`
  接收的结构不变，仍可复用；`_multimodal_image_rail_params` / `_member_model_name` 与 rail
  挂载逻辑不动。
- **回归用例**：`test_capability_resolution_skips_entry_copy_and_decryption`（探针对象
  `__deepcopy__` 即报错）、`test_config_without_models_section_disables_native_image`、
  `test_legacy_single_default_entry_is_honored`；既有 `test_member_model_entry_matches_by_model_name`、
  `test_leader_gets_multimodal_rail_and_capability_flag`、
  `test_explicit_spec_flag_wins_over_modalities` 等继续通过。

### 验证

- 定向复跑（`-o addopts="--asyncio-mode=auto"`）：`tests/unit_tests/agents/harness/common/multimodal/`
  + `tests/unit_tests/agentserver/test_team_multimodal_round_manifest.py` +
  `tests/agents/swarm/test_team_multimodal_image.py` = **68 passed**。
- 区域回归：上述范围 + `tests/unit_tests/agents/harness/common/rails/` + `tests/agents/swarm/`
  + `tests/unit_tests/agentserver/test_team_ask_user_resend.py` = **472 passed / 2 skipped**。
- 未覆盖：与首轮修复相同——端到端真实 team 会话出图、分布式 teammate 的 spawn 快照路径
  （本复查未触及）。

## 遗留

- **teammate 原生图片的授权粒度是会话级清单 ∩ 成员引用行**：不是"按任务分配"的精确
  table——leader 若在 `send_message` 里转述了某张上传图的引用行，该成员就能看到这张图。
  授权动作是 leader 的转述，不是 task→paths 映射；若产品要求"只有任务里指定的图才能看"，
  需另建 task→paths 授权表并在物化门里校验（不能靠放宽/收紧现有安全门实现）。
- **teammate 原生需要"图在上下文里被提到"**：成员侧确定性分发扫描文件名 / 完整路径；若
  上下文只剩"第二张图"这类描述、任务看板也不落路径，则连 read_file 也无从定位，成员只能走
  VQA 兜底。要让这类也原生，需要 leader 在任务里落路径（提示词已要求），或另建任务↔上传关联。
- **read_file 原生读图是 tool 往返**：它依赖模型愿意调 read_file（策略段只做引导）；确定性的
  零工具轮路径仍是成员侧自动补引用行。
- **分发兜底只改单播消息**：`to="*"` 广播与 `to="user"` 不改写（避免把上传清单发给无关成员 /
  用户）；需要广播带图时由 leader 自己写引用行（成员侧仍按清单 ∩ 提及放行）。
- **成员侧扫描包含 tool result 文本**：判断"提到了哪张图"会读 tool result，但只允许清单内
  的上传图被物化（不会内联任意本地文件）；新增该行为时保持"路径必须在本轮上传清单内"这一
  安全门，不能放宽为任意路径。
- **子进程 / 分布式 teammate 的清单是 spawn 时快照**：`build_context_seed` 在 spawn 时序列化，
  后续轮次的新上传不会刷新到远端成员；进程内 member 共享 live dict 不受影响。
- **fallback notice 仍按主模型判定**（S_22 N8）：team 分支 `_build_image_tool_fallback_notice`
  用请求主模型能力，而实际作答是 leader 成员模型。若主模型声明 image 而 leader 模型不支持，
  用户不会看到"已切换为图片工具"提示（本修复未改该口径）。
- **成员换模不热更新能力位**：能力位在成员 spec 装配期固定（`build_member_deep_agent_spec`），
  运行中经 `/model` 换到另一种模态的模型不会重算；需要重建 team runtime 才生效。这与单 agent
  侧 `image_capability_sync` 的运行时同步不同，若产品要求 team 内换模即时生效，需另立机制。
- 参考环境 openjiuwen 0.1.16 与仓内 pin 的 `dev-stable` 可能有个体差异，升级后按 S_22
  「修改检查单」复验能力位与 rail 装配语义。
