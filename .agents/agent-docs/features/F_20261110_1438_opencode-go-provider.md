---
id: F_20261110_1438_opencode-go-provider
title: 新增 opencode-go 模型供应商
type: F
author: wang_njust_608
author_email: wang_njust_608@noreply.gitcode.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/common/config_panel
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/server/runtime
  - jiuwenswarm/agents/swarm
  - jiuwenswarm/agents/harness/team/rails
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/channels/tui/frontend
files:
  - jiuwenswarm/common/opencode_go.py
  - jiuwenswarm/common/config.py
  - jiuwenswarm/common/config_panel/models_handlers.py
  - jiuwenswarm/common/config_panel/tui_models_handlers.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/moa_runtime.py
  - jiuwenswarm/agents/harness/team/rails/opencode_session_rail.py
  - jiuwenswarm/agents/swarm/config_specs.py
  - jiuwenswarm/agents/swarm/providers/member_rails.py
  - jiuwenswarm/agents/swarm/registry.py
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelAdapters.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelDialog.tsx
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelGroupAdapters.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelVendorLabels.ts
  - jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts
  - jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/model.ts
  - jiuwenswarm/resources/.env.template
  - jiuwenswarm/resources/config.yaml
  - docs/zh/配置信息.md
  - docs/en/Configuration.md
designs: []
design_note: 需求与方案由本地 Spec Kit 资产 specs/001-opencode-go-provider/ 承载（spec/plan/tasks，仅本地保留、不入库），未另建 .agents/agent-docs 下的 D 编号；上游供应商预设归 agent-gateway 仓。
---

# 新增 opencode-go 模型供应商

## 实施与决策

参考 Issue 6276，新增 `opencode-go` 供应商。它是 OpenAI 兼容方言，身份只由
`model_client_config.vendor_key == "opencode-go"` 判定：`client_provider` 固定为
`OpenAI`，`api_base` 无预设值（必须手填），`models_needs_key=True`。

请求头 `x-opencode-session` 的值为 `sha256("jiuwenswarm:opencode-go:" + scope)`
取前 32 位十六进制，作用域按上下文派生：交互会话为 `session:<id>`，团队成员为
`member:<session>:<成员标识>`，配置探测固定 `probe:config-validate`，其余共享
模型构造为 `aux`，无会话时为 `session:unscoped`。用户已在 `custom_headers` 中
手工配置同名头（大小写不敏感）时一律保留用户值，程序不重复注入。

注入点：适配器 recipe 深拷贝（按会话隔离，不写回 `config` 指纹）、stable/default
模型解析、MoA 运行时、`build_model_from_entry` 兜底（`aux`）、团队成员 rail
（优先级 4，`model_copy(update=...)` 重设 LLM）。Web 端供应商卡片来自上游
`agent-gateway` 的 `model_vendor_registry`；本仓只负责选择后的保存、探测与运行。

与设计不一致的落地决策：

- `_merge_models_for_replace_all` 的保存回归放在
  `tests/unit_tests/test_models_replace_all_merge.py`（该文件已有口令往返用例），
  而不是 tasks.md 写的 `test_model_config_validation.py`。
- `inject_session_into_selection` 对未知 selection 形态（非 `ResolvedModel` /
  `ResolvedModelGroup`）原样返回：既有测试用哨兵对象模拟 resolver，未来新增
  selection DTO 也不应让模型构建路径崩溃。代价是未识别的新 DTO 会先缺少请求头，
  需再补 helper 分支。
- TUI 的 `command.model` 在 gateway 进程中执行的是已安装 `agent_gateway` wheel
  内的副本（文件头注明“Gateway 侧副本，改动请同步两侧”）。本仓改动落在
  `jiuwenswarm/common/config_panel/tui_models_handlers.py`（AgentServer /
  ConfigAdapter 使用），同样的 `vendor_key` 透出与 provider 配对校验需提交到
  上游 agent-gateway，并随 T002 的 rev 升级生效。
- 原计划用于装配断言的 `tests/agents/swarm/test_swarm_assembly.py` 在当前
  checkout 无法收集（导入已删除的 `jiuwenswarm.common.mode_matrix`，属既有破损），
  因此无条件挂载改由新文件 `tests/unit_tests/agents/swarm/test_opencode_session_rail_mount.py` 验证。

## 评审修改（PR #9875）

上游 develop 合入（提交 `d12d6d66d`）后按评审意见收敛了六处实现，一份驳回：

- `modelAdapters.modelDraftToEntry`：新增 `resolvePresetApiBase`。预设自带地址时
  仍然无条件采用；预设把地址交给用户填（`opencode-go`）时照存草稿值，但丢弃
  "原样继承自另一供应商条目"的那一个——即草稿值与 `existing.api_base` 逐字节相同
  且 `existing.vendor_key` 不同。此前 `preset.api_base || next.api_base` 在跨供应商
  切换时会把上一家的地址带过来。判定收窄到"未改动过的继承值"，是为了不误伤用户在
  切换后手填的地址：FR-002 允许用户填任何可用地址，一律清空会违背该条；脏值检查
  的前提也正是那条地址确实来自上一家，而不是用户刚输入的。Decision 8 "空地址预设
  可手填、不被覆盖" 的语义因此保持不变。
- `providers/member_rails._build_opencode_session_rail`：`session_id` 与成员标识
  任一为空时返回 `None`，不挂载 rail。否则 scope 退化为 `member::`，所有会话共享
  同一 `x-opencode-session`。
- `config_specs`：`_REVIEWER_TEAM_RAIL_NAMES` / `_REVIEWER_CODE_RAIL_NAMES` 补入
  `registry.OPENCODE_SESSION`。`build_reviewer_deep_agent_spec` 按白名单收窄 rails，
  缺席即让 review 轮次的模型调用不带会话头。
- `moa_runtime.resolve_moa_settings`：MoA 指纹改为在注入会话头之前计算
  （`unscoped_group`），返回值中的 `core_group` 仍带 header。适配器每轮用无 scope
  的重算结果与实例上的 `_moa_fingerprint` 比对，此前两者必然不同，导致每轮
  `cleanup()` 并重建模型组。
- `resources/config.yaml`：默认模型条目补 `vendor_key: ${VENDOR_KEY:-}`。新装直接
  拷贝模板，已有配置靠 `migrate_config_from_template` 从模板补键（`_deep_merge`
  只沿模板已有的键下探，模板没写的键无从补入）；而 `get_default_models` 只在配置
  里没有 `models.defaults` 时才读 `VENDOR_KEY` 环境变量，本模板总是带着
  `models.defaults`，所以只写 `.env.template` 时该变量只被记录、不生效。
- 测试：新增/更新
  `test_opencode_session_rail_mount.py`（reviewer 白名单、空 scope 不挂载）、
  `test_moa_runtime.py::test_fingerprint_ignores_the_request_scope`、
  `test_config_template_key_coverage.py::test_the_default_model_entry_names_vendor_key`、
  `modelSettings.test.mjs` 的跨供应商地址用例；删除本仓新增 rail 测试里多余的
  `@pytest.mark.asyncio`。

**驳回**：要求给 `test_config_validate_model_mcc.py` 的 async 用例补
`@pytest.mark.asyncio`。`pytest.ini` 已配置 `--asyncio-mode=auto`，且
`.specify/memory/constitution.md` 原则 IV 明确"MUST NOT 额外添加
`@pytest.mark.asyncio`"，补标记反而与仓库约束冲突。

## 第二轮检视整改

dev-reviewer 对 head `277a01001` 出具 REWORK 判定（1 Must Fix、1 Should Fix、6 Nice to Have），逐条处理如下。

**MF-1 uv.lock 锁了过期依赖（已修复）**：合并 develop 期间用
`git checkout HEAD -- uv.lock` 清镜像改写时，`HEAD` 还指向合并前的旧提交，于是整份
lock 被写回旧版——它 pin `agent-gateway` 54e5330d 与已废止的 `openjiuwen-protocol`
子目录，而 `pyproject.toml` 要的是 2c40ac15 与 `e2a`。合并后的入库结果因此
`pyproject` 与 develop 零差异、`uv.lock` 却与 develop 差 +39/−48，属自造的依赖回退。
修法是取 `upstream/develop` 的 `uv.lock`：本 PR 不改动任何依赖，两个文件都应对
develop 零差异，这比"重跑 uv lock"更能保证与上游一致。

**SF-1 Web 保存链缺厂商/传输校验（已修复）**：TUI 的 `_vendor_provider_error` 拒绝
`opencode-go` 配非 OpenAI 传输，Web 的 `models.replace_all` 没有同类校验。运行时
`is_opencode_go_client_config` 只认 `vendor_key`，所以那种条目会把
`x-opencode-session` 注入到没请求它的协议上。把校验下沉为
`model_config_validation.vendor_provider_error`，由 `validate_models_config` 调用——
Web 与 TUI 都已走这条路径，规则因此只有一份定义。TUI 侧改为转发调用，删掉了不再使用的
`is_opencode_go_client_config` 导入。

**NTH-1 模块 docstring 与实现不符（已修复）**：概览说所有注入 helper 纯函数、不改入参，
但 `inject_session_header` 就地写入传入的 mapping（函数自己的 docstring 是对的）。
改为分别说明 `*_into_selection` 先复制、`inject_session_header` 要求调用方传副本。

**NTH-2 rail 签名只含 `id(llm)`（已修复，但无测试可证）**：签名补入
`id(llm.model_config)`。CPython 回收后 id 复用的窗口极窄，我尝试写用例复现，发现
新建 `Model` 实例的 `id` 本身就变，用例在旧签名下同样通过——**它测不到这个风险**，
因此没有保留该用例。改动属防御性加固，实际收益未经验证。

**NTH-5 MoA 无条件序列化（按驳回处理）**：尝试让 `scope=None` 直接复用 `resolved`
省去一次 `model_dump`/`model_validate` 往返，`test_fingerprint_ignores_the_request_scope`
立即失败——两个分支的指纹能相等，正依赖它们**都**经过同一次规范化，绕过它就破坏了这个
不变量。这是本 PR 自己为修 MF 级问题引入的保证，不能为微优化撤销。已在代码注释写明原因。

**NTH-3 / NTH-4 / NTH-6 不改**：NTH-4 报告的定位（`config.py:1180`）指向 memory 配置，
实际 `isFreeModelEntry` 在 `modelGroupAdapters.ts`，且已用 `replace(/\/+$/, '')` 归一尾斜杠，
"带尾斜杠的等价地址不再识别"这一条不成立；NTH-3 与 NTH-6 都描述既存交互模式或刻意设计，
与本功能无因果关系，留给后续统一处理。

## Web 界面验证期的三处调整

本地 Web 走查暴露两个问题，一个在本仓、一个在上游预设，处理方式不同：

- **图标回退（本仓，已提交）**：`ModelProviderSelect` 的选中项与下拉项都自己调用
  `getVendorLogoUrl`，返回 `undefined` 时渲染 `{logo ? <img/> : null}`，即整块图标区
  空白；`getModelLogoUrl` 却有 `?? customModelIcon` 兜底，两处口径不一致。给
  `ModelProviderSelect` 补同样的兜底后，没有内置资源的厂商显示通用模型图标，而不是
  什么都不画。`icon_key` 不参与前端渲染（`settingsRefactor.test.mjs` 明令
  `doesNotMatch(modelProviderSelect, /preset\.icon_key/)`），所以按 vendor_key 兜底
  是唯一可行的修法；`modelSettings.test.mjs` 补了两条断言锁住这个兜底。
- **地址占位符（本仓，已提交）**：地址留空的预设，输入框原先只显示通用提示
  「例如：https://api.openai.com/v1」，对 OpenCode Go 是错的示例，而地址正是
  FR-002 要求用户填的那一项。新增 `getPresetApiBasePlaceholder`：按 vendorKey 取
  `settingsPanel.models.apiBasePlaceholders.<vendor>`，取不到退回通用提示，因此后续
  新增预设不加文案也不会比改动前差。文案放在 `vendors` 的兄弟键而不是嵌套进
  `vendors`，避免"遍历 vendors 取厂商列表"这类用法误把占位符当厂商名。
- **预设形状（上游，本地覆盖，不提交）**：`validate` 用的 `opencode-go` 预设在本地被
  临时改成 `CODING_PLAN` + 预填地址 + 6 个模型 + `/v1/models` 拉取端点，与
  `contracts/vendors-list.md`（`CUSTOM_API` / `api_base=""` / `model_options=()` /
  `models_endpoint=None`）相反。后果是 `ModelDialog` 的 `if (custom || !preset?.api_base)`
  判定为假，对话框不渲染 api_base 输入框，用户改不了地址，违背 FR-002。为走查把本地
  `.venv` 里的 `model_vendor_registry.py` 覆盖成契约形状（文件内有 `LOCAL VERIFICATION
  OVERLAY` 注释），走查结束用同目录 `model_vendor_registry.py.bak-pinned` 还原。本仓
  没有为此改任何代码：契约要求的是上游预设形状，改这里等于把上游数据复制一份进本仓，
  与宪法 V 的预设单一来源相冲突（tasks.md T047 记的正是这件事）。

## 验证

- 后端定向：`tests/unit_tests/common/test_opencode_go.py`、`test_config_validate_model_mcc.py`、
  `test_available_models_merge.py`、`tests/unit_tests/agentserver/test_model_recipe_preparation.py`、
  `tests/unit_tests/agentserver/rails/test_opencode_session_rail.py`、`tests/unit_tests/agentserver/test_moa_runtime.py`、
  `tests/unit_tests/gateway/test_cli_channel_handlers.py`、挂载与设计 profile 测试均通过。
- 兜底回归：`uv run pytest tests/unit_tests/common tests/unit_tests/agentserver/test_model_recipe_preparation.py tests/unit_tests/agentserver/rails tests/unit_tests/agentserver/test_moa_runtime.py tests/unit_tests/gateway/test_cli_channel_handlers.py`
  → 1423 passed、3 skipped、16 failed。16 个失败均为既有环境问题：`test_windows_cli_paths.py`
  的 Windows 文件锁 `PermissionError`，`test_jev_model_clear.py` 以 GBK 读取含 UTF-8 中文的
  `resources/config.yaml`（改动前的 HEAD 版本同样失败）。
- 前端：Web `npm run test:model-picker` 59/59；`npm run test:settings-panel` 68/69（唯一失败为
  既有 `ModelsSettings.tsx` 静态扫描断言，与本次改动无关）；TUI `npm run build && npm test`
  全绿。
- 真实上游：用用户临时提供的 Key 对 `https://opencode.ai/zen/go/v1` 完成探测与直连调用，
  模型 `deepseek-v4.1-flash`；Key 未写入仓库、夹具、日志或截图，验证后已清除。
- 会话头：本地抓包服务确认同一会话 10 次请求头一致、跨会话不同、重启后同会话不变，
  其他供应商不带该头。

## 遗留

- 上游 `agent-gateway` 的 `opencode-go` 预设与 gateway 侧 `command.model` 副本尚未合入，
  Web 供应商下拉与 TUI gateway 本地执行路径需等上游发版后升级 rev（tasks.md T002）。
- 未做完整团队与定时任务端到端验证；仅以 rail 与 recipe 单测覆盖成员作用域与重建稳定性。
- 未通过真实 Web/TUI 界面逐项点击走查 §2、§5，改由前端测试、handler 测试与真实上游探测覆盖。
