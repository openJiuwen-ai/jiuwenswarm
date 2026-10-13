# SkillTurbo 技能路由与注册规约（S_02）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_02 / skill-routing-and-registration |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/`（environment.py、planner.py、skill_meta.py、turbo_package_loader.py、tools_loader.py、skill_turbo_tools.py） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无（首批 specs） |

## 范围 / 边界

### 本规约管什么

技能从"磁盘存在"到"可被路由命中"的完整链路：

- **三条技能注册路径**：config `tool_cards` 直接注册、内置 skill_codes 目录扫描（`environment._scan_skills_dir`）、外部 turbo 扫描（`environment._scan_external_turbo_skills`），以及两条类级扫描缓存。
- **skill_root 三级解析链**（`environment._resolve_skill_root`）。
- **Skill 数据模型**（8 字段）与 meta.json 路由元数据规范（`skill_meta.load_skill_meta`）。
- **入口文件发现 5 级优先级链**（`skill_meta.find_skill_root_file`）与三消费方收口。
- **plan_code 组装**（`planner._build_skill_plan_code`）：`from {module} import root` 唯一形态。
- **planner 路由路径**：前置检查、流式 LLM 调用、解析重试边界、置信度阈值、路由 prompt 的准入/排除规则结构。
- **入口工具 `skill_acceleration_exec`** 的对外契约：query 语义、超时豁免、工具描述动态渲染、两块临时拦截层。
- **turbo 内部工具装载**：tools_loader 10 分组、`send_file_to_user` 每请求刷新、tool_info.json 导出。
- **enable_for_models 与 vendor_map** 两个模型白名单的语义区分。

### 本规约不管什么

| 子主题 | 归属 spec |
|---|---|
| plan_code 校验后的执行主链路（exec 加载、回调绑定、流式执行） | [[S_01]] |
| validator 校验规则、执行期沙箱、权限审批 | [[S_03]] |
| PlanNode 节点契约（回调槽、run/run_stream 模板方法） | [[S_04]] |
| 动态包注册机制内部（ensure_turbo_package 碰撞、HITL resume 重注册） | [[S_05]] |
| HITL 中断与 resume 语义（断点保存、重放跳过） | [[S_06]] |
| 降级体系全景（fallback 子代理、FallbackContractError 传播） | [[S_07]] |
| 事件流协议（chunk 过滤/映射/缓冲） | [[S_08]] |
| 外层装配（interface_deep 工具注册、rail 挂载、config 构建） | [[S_09]] |

## 不变量

1. **注册三路径固定顺序，外部覆盖内置**：`environment._load` 依次执行 config `tool_cards` 注册 → `_scan_skills_dir`（内置）→ `_scan_external_turbo_skills`（外部）；同名 Skill **外部版本胜出**（后写覆盖）。`register_skill` 是唯一注册入口，覆盖时打 info 日志。
2. **入口判定唯一实现**：`skill_meta.find_skill_root_file` 是 5 级优先级链的唯一实现；`environment._scan_skills_dir`、`SkillTurboPlanner._find_skill_root_file`、`skill_turbo_tools._build_tool_description` 三个消费方一律委托本函数，禁止自建候选列表（漂移后果：已注册但工具描述缺失，或描述可见但路由不可达）。
3. **plan_code 形态唯一**：`from {module} import root` 单条 from-import。外部技能模块名 = `{package_name}.{skill_name}.{root_stem}`（动态包名，经 [[S_05]] `dynamic_package_prefix` 派生，禁止手写 `skill_turbo_codes_` 字面量）；内置技能 = `{skill_code_import_package}.{skill_name}.{stem}`。该形态同时受 [[S_03]] validator 前缀白名单与 `_safe_import` 双重强制。
4. **路由重试范围边界**：`_ROUTE_MAX_ATTEMPTS = 2`，仅覆盖**解析类瞬态失败**（流式空 content / 非 JSON 文本）；`confidence` 不足、`skill_name = null` 属模型正常判定，**不在重试范围**。把正常判定纳入重试会造成循环降级。
5. **置信度阈值与未注册判定**：`_MIN_ROUTE_CONFIDENCE = 0.6`；confidence 解析失败按 0.0、clamp 到 [0,1]；`skill_name` 为空或未注册 → 返回 None。
6. **skill_name 同步链**：路由命中 → `planner.plan` 调 `env.set_skill_name(skill)`（同步为 meta.json `external_name`，如 pptx-craft）→ `agent._sync_skill_name_to_inputs` 写入 inputs → executor `_merge_env_config_to_inputs` 以 "not in merged" 保护（resume inputs 已有值则不覆盖）。
7. **请求隔离与类级缓存并存**：每次 `skill_acceleration_exec` 调用新建 SkillTurbo/Environment/Executor 实例；`_scan_cache`、`_external_scan_cache`、`_tool_info_last_export` 三个类级缓存跨实例共享。
8. **缓存命中仍须 ensure_turbo_package**：`_external_scan_cache` 命中只跳过扫盘+校验，动态包 sys.modules 注册必须幂等重做（进程内包注册状态与扫描缓存生命周期不同步）。
9. **描述与指南双源同一次扫描**：`skill_turbo_tools.refresh_skill_acceleration_description` 在同一次 `_collect_skill_entries` 结果上重建 ToolCard.description **并**生成指南技能清单，两处永不矛盾。
10. **技能清单绝不闪断**：外部源扫描失败时沿用 `_last_good_entries`（进程级）最近一次成功清单；清单缓存 TTL 60s（`_ENTRIES_CACHE_TTL_SECONDS`，key = 根目录集合 frozenset）。清单为空时指南整段不注入（`remove_section`），禁止注入"暂无可用技能"式空引导。
11. **query 契约**：`skill_acceleration_exec` 的 query 是对**单个任务**的忠实总结——严格基于用户原话与对话上下文，仅做凝练与指代消解，不得扩写脑补（如擅自罗列章节大纲）；多任务必须逐个串行调用，严禁并行。
12. **workspace_base 显式性**：`inputs["workspace_base"]` 仅当 `request_metadata["output_dir"]` 非空时设置；**禁止任何 agent workspace 兜底**（产物路径跑偏的直接来源）。
13. **工具执行按全局 tool_id 现取 + 请求级路由**：`environment.get_tool_function` 从 `Runner.resource_mgr` 现取实例（全局 tool_id 后注册覆盖前者）；携带 session 路由的工具（send_file_to_user）执行期必须从请求级 ContextVar 解析，**禁止依赖实例字段**。因此 `load_send_file_tools` 每请求单独刷新（先 remove 旧 tool_id 再 add）。
14. **mtime 计算失败 = 禁用缓存**：`_compute_skill_codes_mtime` 失败返回 0.0，缓存 key 不匹配 → 每次强制重扫。
15. **enable_for_models 与 vendor_map 互不影响**：前者是 DeepAgent 层 ProgressiveToolRail 的工具懒加载白名单，后者是 thinking 注入的模型白名单（见"接口契约"末节）；新模型接入必须**同时核对两处**。
16. **PlanGenerationError 是无 raise 点的已导出符号**：定义于 planner.py 并经 `__init__.py` re-export，但当前代码无任何抛出点（plan 失败统一返回 None 走降级）。设计不得假设其被抛出，捕获它属于死代码。

## 接口契约

### SkillTurboPlanner（planner.py）

```python
class SkillTurboPlanner:
    _MIN_ROUTE_CONFIDENCE = 0.6
    _ROUTE_MAX_ATTEMPTS = 2

    async def plan(self, task: str, context: dict | None = None) -> str | None
    async def match_skill(self, task, context=None) -> Skill | None
    def build_plan_code(self, skill: Skill | str) -> str | None
```

- `plan`：空 task → None；路由命中后先 `env.set_skill_name(skill)` 再组装 plan_code；无匹配返回 None（由 agent 层抛 `SkillTurboNotHandled("无匹配skill")` 降级，见 [[S_07]]）。
- `match_skill` 内部前置检查（任一命中 → None）：无注册 skill；`model_client` 缺失。
- LLM 调用必须是**流式**（`client.stream`）：部分模型服务对非流式调用存在路由注册限制（触发 429 Route missed），流式规避。
- 解析经 `extract_llm_json`，期望结构 `{"skill_name": str|null, "confidence": float, "reason": str}`。
- 路由 system prompt（`_build_route_messages`）当前内嵌 **PPT 准入规则 5 条**（新建空白 / 主题生成整套 / 文档转 PPT / 调整大纲文案逻辑重生成 / 以旧 PPT 为参考重做新版）与**排除规则 7 条**（寒暄问答 / 纯总结 / 普通文章 / 仅图片 / 直接编辑原 PPT / 调度类诉求即便提及 PPT 也判 null / `__skill_turbo_prior_artifacts__` 增量判定——迭代调整已有产物不参与候选，任一关键参数不同属全新任务正常参与）。新增技能家族时必须把规则重构为按 skill 组织的准入/排除结构，不允许继续堆叠单技能段落。

### skill_meta 模块（skill_meta.py，无引擎内部依赖、可独立测试）

```python
META_KNOWN_KEYS = ("external_name", "description", "match_keywords")

def load_skill_meta(skill_name: str, skill_dir: Path) -> dict[str, Any]
def find_skill_root_file(skill_dir: Path) -> Path | None
```

- `load_skill_meta`：meta.json 含任一已知键的有效 JSON dict 即视为合法（仅含 `external_name` 也可路由）；缺失字段按默认值派生：`external_name ← skill_name.replace("_", "-")`、`description ← f"{skill_name} 任务流"`、`match_keywords ← [skill_name]`。无文件 / OSError / ValueError / 不含任何已知键 → 返回 `{}`，调用方走默认值。
- `find_skill_root_file` 5 级优先级链（seen 去重后取首个存在）：
  1. `{skill_name}_gen_root.py`（精确生成器入口）
  2. `{skill_name}_root.py`（精确根入口）
  3. 任意 `*_gen_root.py`（按文件名 sorted）
  4. 任意 `*_root.py`（按文件名 sorted）
  5. `plan_code.py`（兜底入口）

### turbo_package_loader 发现面（细节归 [[S_05]]）

```python
def discover_external_turbo_skills(skill_roots=None) -> list[ExternalTurboSkill]
def dynamic_package_prefix(skill_name: str) -> str   # "skill_turbo_codes_{skill}." 唯一来源
```

- 约定路径即协议：`{skill_root}/{external_name}/turbo/turbo_codes/{skill_name}/` 下存在入口文件即视为加速面，无技能清单硬编码。
- `skill_name` 以 `_` / `.` 开头的子目录跳过；同 skill_name 多源按 roots 顺序取第一个。
- `dynamic_package_prefix` 是动态包前缀唯一来源，validator 白名单与校验器构造统一经它取值。

### SkillTurboEnvironment 注册面（environment.py）

```python
def register_skill(self, skill: Skill) -> None        # 唯一注册入口，同名覆盖
def register_tool(self, tool_card) -> None
async def register_tools(self, context: ToolLoaderContext | None = None) -> None
def has_skill(self, name) -> bool / get_skill(self, name) -> Skill | None
def set_skill_name(self, skill: Skill) -> None
def reload(self) -> None                              # 清 _skills 重跑 _load，不清类级缓存
def get_tool_function(self, tool_name) -> Callable | None
```

- `register_tools`：静态工具首次调用幂等加载（`load_all`）+ `_refresh_send_file_tools` 每请求刷新 + `_export_tool_info` 导出。
- `get_tool_function`：按全局 tool_id 从 `Runner.resource_mgr` 现取实例，优先 `tool.invoke(inputs)` 协议，回退 `tool.run(**kwargs)`。

### 入口工具 skill_acceleration_exec（skill_turbo_tools.py）

- `@tool(name="skill_acceleration_exec", stateless=True)`，函数名 `skill_turbo(query: str)`；每次调用新建 SkillTurbo 实例（请求隔离）。
- **超时豁免**：`card.properties["resilience"] = {"timeout_s": None}` 豁免 AbilityManager 默认 300s 工具超时（PPT 流水线常超），仍受 `MAX_TOOL_CALL_TIMEOUT_HARD_LIMIT`（3600s）硬上限约束。
- **两块临时入口拦截**（均标注删除方式，属待退役层）：
  - `[TEMP-TEMPLATE-BYPASS]`（skill_turbo_tools.py:828-856）：正则匹配"模板（目录/主文件/路径）+ 绝对路径"或字段名（模板目录路径 / 模板主文件路径 / pack_dir）→ 返回 `success=false` 引导走 skill_tool 标准流程；
  - `[REGION-EDIT-BYPASS]`（skill_turbo_tools.py:858-886）：query 命中 `region_edit.REGION_EDIT_KEYWORDS`（12 个选区/编辑关键词，含空格与无空格两种 class 写法）→ 拦截。
  - 两块均为**语义层启发式**，可被 LLM 措辞变化击穿；主防线在 SkillTurboPromptRail 的排除提示词（[[S_09]]），两层引用**同一词源** `region_edit.REGION_EDIT_KEYWORDS`，禁止各写一份关键词。

### enable_for_models 与 vendor_map（易混点，必须区分）

| 维度 | enable_for_models | vendor_map |
|---|---|---|
| 机制 | DeepAgent 层 ProgressiveToolRail 工具**懒加载**白名单（`progressive_tool_rail.py` 构造参数，`_is_lazy_load_enabled_for_model`） | thinking 参数**注入**的模型白名单（`common/thinking/vendor_map.py`） |
| 配置 | `config.yaml react.tool_lazy_load.enable_for_models`，当前默认 `["glm"]` | 代码内 GLM-5.x 窄正则（防误匹配 glm-4 / glm-50）+ DeepSeek keyword 全版本 |
| 匹配语义 | **空白名单 = 全部模型启用懒加载**；非空对 model_name 做小写子串匹配，不匹配 → 懒加载整体 bypass（全部工具常驻） | 白名单外模型不注入 thinking kwargs |
| 影响 | 不影响 thinking | 不影响工具加载 |

同一模型可以只在其中一个白名单内（如 deepseek 在 thinking 白名单内但不在懒加载白名单内 → 工具全常驻）。**新模型接入 checklist：两处 + vision/audio 等能力 config 三项都要核对。**

## 数据结构

### Skill dataclass（environment.py，8 字段）

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 技能名（= skill_codes 子目录名） |
| `description` | str | meta.json description 或默认 `f"{name} 任务流"` |
| `skill_md` | str | 保留字段，恒为空串 |
| `plan_code` | str \| None | 内置扫描时组装；外部 turbo 恒 None（planner 动态组装） |
| `match_keywords` | list[str] | meta.json 或默认 `[name]`；仅作路由 payload 描述输入 |
| `external_name` | str | 外部技能目录名（meta.json），如 pptx-craft |
| `package_name` | str | 动态包名（如 `skill_turbo_codes_ppt`），内置注册为空 |
| `turbo_codes_dir` | str | turbo_codes 目录绝对路径，内置为空 |

### 类级缓存（跨实例共享，均无淘汰机制）

| 缓存 | key | value | 失效条件 |
|---|---|---|---|
| `_scan_cache` | skill_codes 目录绝对路径 | (max .py mtime, Skill 列表) | mtime 快照变化；计算失败 0.0 强制重扫 |
| `_external_scan_cache` | roots 绝对路径 frozenset | (各 turbo_codes 目录 mtime 快照 dict, Skill 列表) | 任一目录 mtime 变化（→ rebind，见 [[S_05]]） |
| `_entries_cache`（skill_turbo_tools） | 根目录集合 frozenset | (时间戳, {external_name: description}) | TTL 60s |
| `_last_good_entries`（skill_turbo_tools） | —（单值 dict） | 最近一次成功清单 | 仅被新的成功扫描覆盖；外部源失败时沿用 |
| `_tool_info_last_export` | 导出路径 | 上次导出内容串 | 内容变化才写盘 |

### tool_info.json 导出（`environment._export_tool_info`）

- 路径：`config["tool_info_export_path"]` 或 `<get_user_workspace_dir()>/tool_info.json`。
- 写盘协议：内容与上次一致则跳过（去重）→ 临时文件 + `os.replace` 原子写 → 失败仅 WARNING 不阻塞。
- 消费方：CodeGen（skill-turbo-converter）read_file 查询工具 schema 的 ground truth；多实例写同一默认路径时最后写者胜，编排上须保证各实例工具集一致。

## 执行路径全景

### P1 注册链（Environment 构造期，`_load`）

```
config tool_cards → register_tool(s)                     # 路径 1
→ _scan_skills_dir()                                     # 路径 2：内置
    每 skill_codes 子目录：find_skill_root_file 入口
    → _validate_skill_code_dir 全目录 AST 校验（[[S_03]]，失败跳过注册）
    → load_skill_meta → _build_plan_code 组装 → register_skill
    （_scan_cache 命中：跳过扫盘+校验，直接复用 Skill 列表）
→ _scan_external_turbo_skills()                          # 路径 3：外部
    roots = resolve_agent_registered_skill_dirs()
    → discover_external_turbo_skills 发现
    → _validator_for_external_skill 校验（[[S_03]]）
    → ensure_turbo_package 注册动态包（[[S_05]]）
    → register_skill（同名覆盖内置）
    （_external_scan_cache 命中：仍须 ensure_turbo_package 幂等重注册）
```

### P2 skill_root 解析（`_resolve_skill_root` 三级）

1. config `skills_dir` 为有效目录 → 用之；
2. 否则 `resolve_agent_registered_skill_dirs()`（utils.py）首目录，其内部顺序：**请求级绑定目录 > `JIUWENSWARM_SHARED_SKILLS_DIRS` 共享目录 > workspace agent skills 目录**；
3. 否则空串。

skill_root 由 executor 注入 `inputs["skill_root"]`（"not in merged" 保护），供 skill_code 定位技能自身资源目录。

### P3 请求入口链（skill_acceleration_exec）

外层 DeepAgent LLM 决策调用 → 两块临时拦截（不命中才继续）→ `adapter.build_skill_turbo_config()`（tool_cards / model_client / fallback_handler / card / llm_concurrency_limit / vision / sys_operation / permissions，**不含 skill_codes_dir**）→ `SkillTurbo(config)` 实例化 → inputs 组装（query / conversation_id / session_id / request_id / channel_id / user_id / chat_id / effective_project_dir / workspace_base（仅 output_dir 非空时）/ metadata）。

### P4 路由（`planner.plan` → `_match_skill`）

前置检查（空 task / 无注册 skill / model_client 缺失 → None）→ 构造 skills_payload（name/description/match_keywords）+ 路由 messages → 流式 LLM 调用（attempt 1..2，仅解析类瞬态失败重试）→ `extract_llm_json` → skill_name 空判定 → confidence < 0.6 判定 → 未注册判定 → 命中返回 Skill。

### P5 plan_code 组装（`_build_skill_plan_code`）

`_find_skill_root_file`（外部技能查 `{turbo_codes_dir}/{skill_name}/`，内置查 `{skill_codes_dir}/{skill_name}/`，均委托 skill_meta 收口实现）→ 命中 → `_to_skill_root_module` 派生模块名 → `from {module} import root`；未命中 → `skill.plan_code` 显式配置兜底；仍无 → None。

### P6 降级

plan 返回 None → `agent.run_stream` 抛 `SkillTurboNotHandled("无匹配skill")` → 工具层包装 `success=false` → 外层 LLM 依提示词指引转 skill_tool 标准流程（降级全景见 [[S_07]]）。

### P7 工具描述动态渲染（请求期）

SkillTurboPromptRail 每次 `before_model_call` 调 `refresh_skill_acceleration_description()`：`_collect_skill_entries`（双源：内置 skill_codes 目录 + 外部 turbo 目录，同一根目录集合 60s TTL 缓存）→ 外部源失败沿用 `_last_good_entries` → 同一结果重建 ToolCard.description + 指南技能清单 → 空清单整段不注入。

### P8 工具装载（executor 开头 `env.register_tools`）

`build_tool_loader_context`（agent_id 默认 "skill_turbo" / language / sys_operation / vision / audio / skill_manager / request 级三元组）→ `load_all` 10 分组（`_register_group` 统一 try/except 异常隔离 + card.id 校验 + `Runner.resource_mgr` 去重后 add_tool；可用性判断 100% 复用上游 `is_xxx_enabled` / 工厂分支，不引入新算法）：

| 组 | loader | 可用性条件 |
|---|---|---|
| named_web | `_load_jw_named_web` | 始终（web_search / fetch_webpage） |
| vision | `_load_jw_vision` | `vision_model_config` 非 None |
| audio | `_load_jw_audio` | create_audio_tools 内部按 config 过滤 |
| video | `_load_jw_video` | `video_model_enabled` |
| image_gen | `_load_jw_image_gen` | `image_gen_enabled` |
| skill_toolkit | `_load_jw_skill_toolkit` | `skill_manager` 非 None |
| ask_user | `_load_jw_ask_user` | 始终注册（ImportError 容忍为空） |
| deepresearch | `_load_jw_deepresearch` | 始终注册（ImportError 容忍为空） |
| filesystem | `_load_oj_filesystem` | `sys_operation` 非 None（ReadFile/WriteFile/EditFile/ListDir/Glob/Grep） |
| bash | `_load_oj_bash` | `sys_operation` 非 None（BashTool） |

`load_send_file_tools`：前提 `request_id and session_id`；**正典装载条件是 `config.channels[channel].send_file_allowed` 能力位**（新增 channel 一律配置该开关）。代码现状另有 `channel == "officeclaw"` 历史兼容旁路（tools_loader.py，待删除的历史遗留，勿写成正典契约、勿在新 channel 上依赖）；**每请求 remove + add**。

### P9 resume 旁路

HITL resume 不走 planner：直接使用持久化的 `resume_ctx["plan_code"]`（动态包由 `ensure_packages_for_plan_code` 自愈重注册，[[S_05]] / [[S_06]]）；skill_name 经 "not in merged" 保护从 inputs 还原，不依赖路由重放。

## 异常分支与边界 case

| # | case | 行为 / 风险 | 测试覆盖 |
|---|---|---|---|
| 1 | 同名技能多源 | roots 顺序（请求级 > 共享 > 工作区）取第一个，后续源同名静默忽略 | 部分：test_turbo_package_loader（多 roots 发现；同名取首个无断言） |
| 2 | 清洗碰撞 | 不同 skill_name 清洗后同包名（如 "p.pt" 与 "p_pt"）→ `ensure_turbo_package` 拒绝后注册者 → 跳过注册（[[S_05]]） | test_turbo_package_loader（collision_after_sanitize_rejected） |
| 3 | mtime 计算失败 | 返回 0.0 → 缓存永不命中 → 每次全量重扫（正确性优先） | 无 |
| 4 | skill_name 以 `_` / `.` 开头 | 发现阶段跳过；`__pycache__` / `.git` 在 mtime 计算与校验中排除 | 无 |
| 5 | meta.json 缺失/损坏/无已知键 | `{}` + 默认 description/keywords，技能仍可注册路由 | test_environment_skill_meta |
| 6 | 入口文件全缺失 | find_skill_root_file None → `skill.plan_code` 兜底 → 仍无 → "无匹配" 降级 | 部分：test_turbo_package_loader（发现侧跳过）；planner 兜底链无 |
| 7 | 路由 LLM 空输出 / 非 JSON | 瞬态失败重试 1 次；仍失败 → None 降级（非错误路径） | test_planner_route_retry |
| 8 | confidence 不足 / skill_name=null | 模型正常判定，**不重试**，直接 None | test_planner_route_retry |
| 9 | 拦截层语义击穿 | 两块 TEMP 拦截为正则/关键词启发式，LLM 改写 query 可绕过；主防线在 PromptRail 提示词 | test_entry_gates_and_honesty（拦截正/负例） |
| 10 | 外部源扫描瞬时失败 | `_last_good_entries` 兜底，清单不闪断为"暂无"；ToolCard 并发最后写生效（per-user sidecar 内目录一致，无实际冲突） | test_description_refresh（last-good 用例） |
| 11 | `reload()` 不清类级缓存 | 目录变更靠 mtime 快照失效兜住；但旧 key 永久驻留（无淘汰），长驻进程内存缓慢增长面 | 无 |
| 12 | tool_info.json 多实例同路径 | 最后写者胜；CodeGen 可能读到其它实例的 schema，编排须保证工具集一致 | test_environment_export_tool_info |
| 13 | 路由规则单技能硬编码 | PPT 准入/排除写死在 system prompt；新技能家族接入须重构规则结构，否则规则互相污染 | 无（结构性债务） |
| 14 | PlanGenerationError | 已定义 + re-export 但无 raise 点；按死符号对待，不得在其 except 分支上堆积逻辑 | 无（死符号） |
| 15 | model_client 缺失 | 路由直接 None（仅 warning），后续表现为"无匹配"降级 | test_planner_route_retry（TestPlannerPrechecks） |
| 16 | 空 task / 空 query | plan 前置检查返回 None 降级 | test_planner_route_retry（TestPlannerPrechecks） |

## 修改检查单

- **改入口优先级链** → 只改 `skill_meta.find_skill_root_file` 一处；必查三消费方（注册扫描 / planner 组装 / 工具描述）输出一致，并同步 `planner._build_skill_plan_code` docstring 的入口约定描述。
- **改路由重试或阈值** → 必查重试范围不含 confidence 不足 / skill_name=null（防循环降级）；同步 `_ROUTE_MAX_ATTEMPTS` 日志口径与测试。
- **改准入/排除规则** → 必查 `__skill_turbo_prior_artifacts__` 增量判定分支语义不被破坏；多技能场景先重构为 per-skill 规则结构再增规则。
- **改 tools_loader 分组** → 必同步 [[S_03]] 审批卡清单 `SKILL_TURBO_APPROVAL_TOOLS` + tool_info.json 消费方 + 回归测试 `test_skill_turbo_approval_tools.py`。
- **改拦截关键词/正则** → 必查双防线同源（`region_edit.REGION_EDIT_KEYWORDS`）+ SkillTurboPromptRail 排除提示词同步；删除拦截块前先确认主防线覆盖。
- **新模型接入** → checklist：`enable_for_models`（懒加载白名单）/ `vendor_map`（thinking 白名单）/ vision 等能力 config 三处逐一核对，缺一即行为分裂。
- **改外部 turbo 约定路径** → 必同步 `discover_external_turbo_skills` 发现协议、`ensure_turbo_package` 注册、validator 前缀派生（[[S_03]] / [[S_05]]）三处。
- **改 skill_root 解析** → 必查 `resolve_agent_registered_skill_dirs` 三级顺序与请求级绑定时序（绑定晚于解析则首目录取错）。
- **改扫描缓存结构** → 必保 mtime 快照失效语义 + 缓存命中路径仍执行 `ensure_turbo_package` 幂等重注册，二者缺一即"已缓存但 import 失败"。
- **改 plan_code 形态** → 必同步 [[S_03]] validator 前缀白名单与 `_safe_import`（双重强制），以及 `ensure_packages_for_plan_code` 的 import 正则（[[S_05]]）。
- **改 send_file 装载** → 必保每请求 remove + add 与请求级 ContextVar 路由；引入任何依赖实例字段的工具前先评估并发覆盖面。
- **改 query 契约 / 工具描述文案** → 必保 `_render_tool_description` 与指南清单同一次扫描产出（不变量 9）；单任务串行约束在描述与 docstring 双处声明。

## 与其它 spec 的关系

| Spec | 关系 |
|---|---|
| [[S_01]] | 本 spec 产出 plan_code 与注册表，是 S_01 执行主链路的输入；执行期 inputs 注入（skill_root/skill_name 的 "not in merged" 保护）在本 spec 定义同步链，S_01 定义消费时机。 |
| [[S_03]] | 注册扫描调用 validator 做全目录校验（校验拒绝 → 跳过注册，表现为本 spec 的"无匹配"降级）；plan_code 形态受其前缀白名单强制；审批卡清单与本 spec 工具分组互为同步对象。 |
| [[S_04]] | 入口文件暴露的 `root: PlanNode` 契约、display_names 等节点侧约定归 S_04；本 spec 只负责找到入口并组装 import。 |
| [[S_05]] | 外部 turbo 的动态包注册、包名派生、清洗碰撞、HITL 重注册机制归 S_05；本 spec 只定义发现协议路径与调用时序。 |
| [[S_06]] | resume 不走 planner（本 spec P9）；resume_ctx 中持久化的 plan_code 由本 spec 的组装规则产出。 |
| [[S_07]] | 路由级降级（None → SkillTurboNotHandled）是 S_07 降级体系的第一级；重试边界扩大化的风险（循环降级）两 spec 共同守护。 |
| [[S_08]] | 工具描述渲染与 PromptRail 清单注入的时序（before_model_call）影响事件流前置内容，事件协议本身归 S_08。 |
| [[S_09]] | `skill_acceleration_exec` 的工具卡注册、PromptRail/PermissionRail 挂载、`build_skill_turbo_config` 装配归 S_09；本 spec 定义工具的对外契约与渲染内容。 |
