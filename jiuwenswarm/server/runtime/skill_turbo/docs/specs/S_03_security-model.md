# SkillTurbo 安全模型规约（S_03）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_03 / security-model |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/`（validator.py、executor.py、environment.py、rails/permission_rail.py、plan_node.py、runtime/__init__.py） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无（首批 specs） |

## 范围 / 边界

### 双层防御总架构

SkillTurbo 对 skill_code / plan_code 的安全控制由**两层独立防线**构成：

1. **注册期静态 AST 校验**（validator.py，`PlanCodeValidator` + `CodeValidationPolicy` 三 profile）：技能目录扫描时对全部 `.py` 做 `ast.parse` + 遍历检查，任一文件违规 → 整技能跳过注册（见"执行路径全景"P1/P2）。
2. **执行期动态沙箱**（executor.py，`_build_namespace` / `_safe_import` / `_extract_root_node`）：plan_code 经 `exec` 执行时运行在受限 namespace 中，builtins 白名单封闭、`__import__` 被替换为受控实现。

两层职责对照：

| 维度 | 注册期静态层（validator） | 执行期动态层（沙箱） |
|---|---|---|
| 检查对象 | 技能目录全部 `.py` 源码（含被相对导入者） | 单条 plan_code 的 exec 运行时 |
| 检查手段 | AST 遍历（不执行代码） | 受限 namespace + 受控 import |
| 拦截面 | import 白/黑名单、危险函数/属性调用、dunder、try 守卫、语法结构 | builtins 白名单、`__import__` 替换、fromlist 黑名单、根节点提取 |
| 失败后果 | 整技能不注册（用户见"无匹配skill"） | PlanCodeValidationError / PlanCodeLoadError（流式 error 收口） |

两层**独立存在、互不替代**：静态层管"哪些代码能注册进来"，动态层管"即使代码形态合法，运行时也只能触达白名单能力"。此外第三道治理面是**审批模型**：turbo 内部工具调用收口到外层一次性授权（P5/P6）。

### 本规约管什么

- `CodeValidationPolicy` 三 profile 的全参数契约与清单内容。
- AST 检查规则表、`_is_import_allowed` 判定序、`require_abort_reraise` 静态强制。
- 相对导入的安全性论证（动态包受控 `__path__` + 目录全量校验）。
- 校验失败的两种链路（注册期 / 执行期）及用户可见后果。
- 执行期沙箱：`_SAFE_BUILTINS`、`_safe_import`（含 fromlist 黑名单）、`_extract_root_node`。
- 审批模型：外层一次性授权、SkillTurboPermissionRail 统一审批卡、fallback 子代理权限挂起。
- 安全护栏的 fail-closed 语义与 AbortError 类型同一性。

### 本规约不管什么

| 子主题 | 归属 spec |
|---|---|
| 路由命中、注册路径、工具装载分组 | [[S_02]] |
| PlanNode 节点契约（回调槽、run/run_stream 模板方法、运行期 AbortError 三道穿透的行为细节） | [[S_04]] |
| 动态包注册机制内部（ensure_turbo_package、包名派生、门面 CONTRACT_VERSION） | [[S_05]] |
| HITL 中断与 resume 断点语义 | [[S_06]] |
| fallback 契约解析、validate_fallback_success 双层判定的降级语义 | [[S_07]] |
| 工具调用事件补发（chat.tool_call / chat.tool_update） | [[S_08]] |

## 不变量

1. **双层防御缺一不可**：任何"静态校验已覆盖，运行期可以放开"或反向的改动均违反本架构；两层清单变更必须分别评估。
2. **黑名单优先于白名单**：`_is_import_allowed` 判定序固定为 denied_exact → denied_prefix → allowed_exact/allowed_prefix；模块同时命中黑白名单时**拒绝**。新增白名单项必须先确认不在黑名单及前缀黑名单内。
3. **清单数量契约**（builtin profile，改清单必须全量过数）：`allowed_import_exact` 14 项、`denied_import_exact` 14 项、`denied_import_prefixes` 3 项（`http.` / `urllib.` / `requests.`）、`denied_call_names` 18 项、`denied_attribute_call_names` 15 项、`allowed_dunder_attributes` 2 项（`__init__` / `__name__`）。
4. **AbortError 类型同一性**：`openjiuwen.core.runner.callback.AbortError` 经 plan_node re-export → runtime 门面 re-export → skill_code `from skill_turbo_runtime import AbortError`，全链**同一个类对象**；任何一环重新定义/包装都会使 `isinstance` / `except` 判定失效、HITL 中断被吞。整个 skill_turbo 与所有 skill_code 一律从 plan_node / 门面取 AbortError，禁止直连 openjiuwen 路径 import（identity 测试 `test_runtime_facade.py` 守护）。
5. **require_abort_reraise 注册期强制**：`plan_code` 与 `builtin_skill_code` 两个 profile 启用；宽泛 except 子句（bare / `except Exception` / `except BaseException`）体内必须存在 `if isinstance(<name>, AbortError): raise`（或 `raise <name>`）守护，未通过 → 注册期拒绝。具体异常类（如 `except ValueError`）不受约束。
6. **注册期校验失败 → 整技能跳过**：技能目录内任一 `.py` 校验失败即该技能不注册；默认仅 WARNING 日志（`strict_builtin_skill_validation` 默认 False，过渡期）；strict=True 时抛 ValueError 阻止启动。
7. **执行期 namespace 封闭**：plan_code 的 exec namespace 仅含 `PlanNode` + `__builtins__ = dict(_SAFE_BUILTINS)` + `__import__ = self._safe_import`，不暴露 use_tool / call_llm 等执行能力（节点经 PlanNode 方法回调访问，见 [[S_04]]）。
8. **_safe_import 三重约束**：禁相对导入（level≠0 → ImportError）+ 模块名前缀白名单（`env.skill_code_import_prefixes`，含动态包前缀）+ fromlist 黑名单 `_DENIED_FROMLIST_NAMES`（15 项，防 `from skill_codes.xxx import __import__` 式绕过）。
9. **相对导入安全性来自机制而非静态检查**：validator 不校验相对导入目标（`deny_relative_import=False` 直接放行）；安全性 = 相对导入只能命中 sys.modules 动态注册包的受控 `__path__`（物理无法逃逸，[[S_05]]）+ 技能目录 `rglob("*.py")` 全量同 profile 校验（包内被导入者必已过检）。
10. **审批收口外层**：内层 executor 不挂权限 rail；`skill_acceleration_exec` 在外层 DeepAgent 经 config `skill_turbo: ask` 一次性授权内部工具，审批通过后内部调用直接放行。
11. **审批卡文案即协议**：SkillTurboPermissionRail 的审批消息必须包含关键字「需要授权」，供 上游权限桥识别为审批卡；改文案不得移除该关键字。
12. **审批清单覆盖实际工具类别**：`SKILL_TURBO_APPROVAL_TOOLS`（11 项）仅用于审批卡展示、无行为耦合，但必须覆盖 turbo 节点实际可用工具类别（**含网络组** web_search / fetch_webpage）；回归测试 `test_skill_turbo_approval_tools.py` 断言网络组存在 + 核心组不回归。
13. **fallback 子代理 suspend/restore 对称**：父子 agent **共享同一权限 rail 实例**，`spawn_fallback` 挂起三类 rail（PermissionInterruptRail / SkillAuthorizationPermissionRail / SkillTurboPermissionRail）时必须 `_static_config.enabled=False` + 替换 `host.get_permissions_snapshot` 回调，且 finally 恢复；漏恢复 = 父 agent 权限状态被子代理污染。
14. **安全护栏 fail-closed**：护栏路径自身异常必须中断流程而非被框架吞掉继续执行（如 `validate_fallback_success` 实现异常同样触发终止性降级，[[S_07]]）；callback framework 只认 AbortError——中断/审批信号以 AbortError 为唯一载体，护栏抛其它异常类型不会被框架识别为中断。
15. **节点不可访问面封闭**：skill_code 节点不可访问 session 对象、事件发射器、request_id、环境变量、白名单外任意 import、直接文件 IO（`denied_attribute_call_names` 封死 pathlib 读写方法）、dunder 属性（`__init__` / `__name__` 除外）。事件发射全部由 executor / rail 层代理。
16. **先 deepcopy 再绑回调**：plan_code import 的是模块级单例，`_prepare_root_node` 必须 `copy.deepcopy(root)` 后再 `_bind_node_callbacks`，否则并发请求互相覆盖回调（并发隔离，与 [[S_01]] 共同守护）。

## 接口契约

### CodeValidationPolicy（validator.py，frozen dataclass）

字段全集：`name` / `allow_import` / `allow_import_from` / `allowed_import_exact` / `allowed_import_prefixes` / `denied_import_exact` / `denied_import_prefixes` / `import_error_hint` / `deny_relative_import` / `deny_dunder_attribute` / `allowed_dunder_attributes` / `denied_call_names` / `denied_attribute_call_names` / `deny_global` / `deny_nonlocal` / `deny_with` / `deny_try` / `deny_del_attribute` / `deny_del_subscript` / `deny_type_three_args` / `require_abort_reraise`。

三个工厂 profile：

**A. `plan_code(allowed_import_prefixes)`** — executor 校验 LLM 组装的 plan_code（executor 以 `env.skill_code_import_prefixes` 构造）：

| 项 | 值 |
|---|---|
| allow_import | False（**禁止一切 `import x` 语句**，只允许 `from x import y`） |
| allow_import_from | True，模块名须命中 allowed_import_prefixes |
| deny_relative_import | True（plan_code 无包上下文） |
| require_abort_reraise | True |
| 其余 deny_* | 默认 False |

**B. `generated_skill_code()`** — 为 LLM 动态生成 skill_code 预留的**最严** profile，当前无调用方，行为冻结：

- allow_import=False；`allowed_import_exact` 仅 1 项：`jiuwenswarm.server.runtime.skill_turbo.plan_node`。
- `denied_call_names` 19 项 = builtin 的 18 项 + `type`（连单参 `type()` 也禁）。
- `denied_attribute_call_names` 15 项（同 C）。
- deny_global / deny_nonlocal / deny_with / **deny_try** / deny_del_attribute / deny_del_subscript / deny_type_three_args **全开**（deny_try=True 时整段 try 禁用，故无需 require_abort_reraise）。

**C. `builtin_skill_code(allowed_import_prefixes)`** — 注册期校验内置与外部 turbo skill_code（environment 扫描构造；外部 turbo 前缀 = 内置前缀剔除动态包前缀 + 该技能自身 `skill_turbo_codes_{skill}.`）：

- allow_import=True；allow_import_from=True；**deny_relative_import=False**（外部 turbo 包内互引）。
- `allowed_import_exact` 14 项全列举：

```
__future__   asyncio   collections.abc   contextlib   dataclasses   datetime
json   logging   pathlib   re   typing
jiuwenswarm.server.runtime.skill_turbo.plan_node   # AbortError/FallbackContractError 唯一合法触点
skill_turbo_runtime                                       # 外部 turbo code 中立门面
skill_turbo_runtime.tool_utils
```

- `denied_import_exact` 14 项全列举：`builtins` `ctypes` `importlib` `inspect` `marshal` `multiprocessing` `os` `pickle` `shutil` `socket` `subprocess` `sys` `tempfile` `threading`。
- `denied_import_prefixes`：`http.` / `urllib.` / `requests.`（禁网络直连；httpx 等其它网络库不在白名单内，经白名单缺省拒绝）。
- `allowed_dunder_attributes`：`__init__` / `__name__`（`deny_dunder_attribute` 默认 True 且本 profile 未关闭，其余 dunder 全禁）。
- `denied_call_names` 18 项：`open` `exec` `eval` `compile` `__import__` `getattr` `setattr` `delattr` `globals` `locals` `vars` `dir` `input` `help` `breakpoint` `exit` `quit` `memoryview`。
- `denied_attribute_call_names` 15 项：`read_text` `read_bytes` `write_text` `write_bytes` `open` `unlink` `rmdir` `rename` `mkdir` `glob` `rglob` `create_subprocess_exec` `create_subprocess_shell` `to_thread` `basicConfig`（文件 IO 必须走工具通道）。
- deny_del_attribute / deny_type_three_args / require_abort_reraise = True。

### PlanCodeValidator

```python
PlanCodeValidator(allowed_import_prefixes=None, policy=None)   # 默认 policy = plan_code(...)
PlanCodeValidator.for_skill_code(allowed_import_prefixes)      # builtin profile
PlanCodeValidator.for_generated_skill_code()                   # generated profile

def validate(self, code: str) -> list[str]        # 空列表 = 通过；语法错误返回 ["语法错误: ..."] 不抛异常
def validate_or_raise(self, code: str) -> None    # 非空抛 PlanCodeValidationError(errors)
```

profile 构造调用方（改 profile 定义必查这四个消费点）：

| 调用方 | profile | 前缀来源 |
|---|---|---|
| `executor.__init__`（`_validator`，消费于 `_prepare_root_node`） | plan_code | `env.skill_code_import_prefixes`（含动态包前缀） |
| `environment._scan_skills_dir` → `_validate_skill_code_dir` | builtin_skill_code | `env.skill_code_import_prefixes` |
| `environment._scan_external_turbo_skills` → `_validator_for_external_skill` | builtin_skill_code | 内置前缀剔除动态包前缀 + `skill_turbo_codes_{skill}.`（[[S_05]] 派生） |
| `PlanCodeValidator.for_generated_skill_code` | generated_skill_code | 无前缀（exact 白名单单列；当前无调用方，预留） |

### AST 检查规则表（`validate` → `ast.walk` → `_check_node`）

| AST 节点 | 规则 |
|---|---|
| Import | allow_import=False → 整体拒绝；True 时逐 alias 过 `_is_import_allowed` |
| ImportFrom | level>0（相对）→ 由 deny_relative_import 决定（False 时直接放行、不校验目标，见不变量 9）；绝对 → allow_import_from + `_is_import_allowed(module)` |
| Attribute | deny_dunder_attribute 且 `attr.startswith("__")` 且不在 allowed_dunder_attributes → 拒绝 |
| Call | func 为 Name 且在 denied_call_names → 拒绝；deny_type_three_args 且 `type(...)` ≥3 参 → 拒绝（动态类型创建）；func 为 Attribute 且 attr 在 denied_attribute_call_names → 拒绝 |
| Global / Nonlocal | 对应 deny 开关 |
| With / AsyncWith | deny_with |
| Try | deny_try → 整体拒绝；否则 require_abort_reraise → `_check_try_reraise` |
| Delete | deny_del_attribute（`del obj.attr`）/ deny_del_subscript（`del obj[key]`） |

`_check_try_reraise` 守护识别（`_is_broad_handler` + `_handler_reraises_abort`）：

- 宽泛判定：bare except / `except Exception` / `except BaseException` 三种；具体异常类（如 `except ValueError`）不触发检查。
- 守护形态：except 体内存在 `if isinstance(<name>, AbortError): raise` 或 `raise <name>`；AbortError 可写作裸名或 `X.AbortError` 属性形式；守护体必须是 raise 语句。
- 识别不到 → 该 handler 记一条违规（中文消息含行号），归入注册期拒绝链路。

### 执行期沙箱接口（executor.py）

```python
executor._build_namespace() -> dict        # {"__builtins__": dict(_SAFE_BUILTINS) + __import__, "PlanNode": PlanNode}
executor._safe_import(name, globals_, locals_, fromlist, level) -> Any
executor._extract_root_node(namespace) -> PlanNode    # staticmethod
```

`_extract_root_node` 三分支：`namespace["root"]` 是 PlanNode → 用之；0 个 PlanNode 实例 → `ValueError("规划代码未生成 PlanNode 实例")`；恰好 1 个 → 兜底采用（脆弱，依赖 dict 迭代顺序，plan_code 应显式 `root = ...`）；>1 个 → `PlanCodeValidationError`（要求显式赋值）。

### 审批接口（rails/permission_rail.py）

```python
SKILL_TURBO_APPROVAL_TOOLS: list[tuple[str, str]]   # 11 项 (name, desc)
class SkillTurboPermissionRail(SkillAuthorizationPermissionRail):
    def _build_message(self, tool_call, result) -> str   # 仅此一处覆盖
```

`SKILL_TURBO_APPROVAL_TOOLS` 11 项：`bash` / `read_file` / `write_file` / `list_dir` / `glob` / `web_search` / `fetch_webpage` / `image_ocr` / `visual_question_answering` / `generate_image` / `send_file_to_user`。非 `skill_acceleration_exec` 工具走父类默认文案；基类携带 Skill 动态授权门禁短路（gate-handled 命中即跳过）。

## 数据结构

### _SAFE_BUILTINS（executor.py）

35 个函数（`len` `str` `int` `float` `bool` `list` `dict` `tuple` `set` `frozenset` `range` `enumerate` `zip` `min` `max` `sum` `abs` `sorted` `any` `all` `round` `reversed` `isinstance` `map` `filter` `repr` `chr` `ord` `hex` `oct` `bin` `divmod` `iter` `next` `slice`）+ 3 个常量（`True` `False` `None`）+ 8 个异常类（`Exception` `ValueError` `TypeError` `KeyError` `IndexError` `RuntimeError` `NotImplementedError` `StopIteration`）。

**剔除面**：`type` / `getattr` / `setattr` / `globals` / `locals` / `vars` / `dir` 等可被滥用的内建一律不进名单；`isinstance` 保留（安全且常用）。新增任何内建前必须评估其逃逸面（能取属性、能执行代码、能建类型的函数禁入）。

### _DENIED_FROMLIST_NAMES（executor.py，15 项）

`__import__` `__builtins__` `__build_class__` `exec` `eval` `compile` `open` `globals` `locals` `vars` `dir` `getattr` `setattr` `delattr` `type` —— 防 `from skill_codes.xxx import __import__` 之类的 fromlist 绕过；`_safe_import` 用 `importlib.import_module` 复刻 `__import__` 语义（fromlist 非空返回 name 模块、为空返回顶层包）。

### PlanCodeValidationError

`PlanCodeValidationError(errors: list[str])`：`errors` 保存全部违规项（中文消息含行号）；`str()` 为 `"规划代码校验失败: [...]"`。经 `__init__.py` 导出。

## 执行路径全景

### P1 注册期静态校验——内置（environment._scan_skills_dir）

`_scan_skills_dir` → `_validate_skill_code_dir(skill_name, skill_dir)`：对技能目录 `rglob("*.py")` 全量（排除 `__pycache__` / `.git`）逐文件用 builtin profile 校验；任一文件失败 → success=False → **跳过该技能注册**（strict 模式抛 ValueError）。扫描结果受 `_scan_cache` mtime 快照控制（[[S_02]]）。

### P2 注册期静态校验——外部 turbo（environment._scan_external_turbo_skills）

`discover_external_turbo_skills` 发现 → `_validator_for_external_skill(skill_name)`：allowed_import_prefixes = 内置前缀（剔除动态包前缀）+ `skill_turbo_codes_{skill}.`（经 [[S_05]] `dynamic_package_prefix` 派生）→ 同样全目录校验 → `ensure_turbo_package` 注册（清洗碰撞被拒同样跳过注册）。

### P3 执行期校验链（executor._prepare_root_node）

`validate(plan_code)`（plan_code profile）→ 失败抛 `PlanCodeValidationError` → **流式入口就地消费**：yield `chat.error`（"规划代码校验失败: {errors}"）+ complete chunk 后正常 return，**不抛 SkillTurboNotHandled**——与非流式 `execute_plan` / agent 层包装 `SkillTurboNotHandled("规划代码校验或加载失败")` 的语义分叉：流式路径外层 LLM 无降级信号，仅见工具"成功"返回但内容为错误（事件语义详见 [[S_01]] E5/E6）。

### P4 执行期沙箱（executor._load_plan_namespace）

`ensure_packages_for_plan_code`（动态包自愈，[[S_05]]）→ `_build_namespace()` → `exec(plan_code, namespace)`（exec 异常 → `PlanCodeLoadError`）→ `_extract_root_node` → deepcopy → 绑回调。

### P5 审批链（外层收口）

1. 外层 LLM 调 `skill_acceleration_exec` → 外层权限链按 config `skill_turbo: ask` 触发审批；
2. `SkillTurboPermissionRail._build_message` 渲染统一审批卡：`SKILL_TURBO_APPROVAL_DESCRIPTION`（一次性授权声明）+ 11 项工具清单 + 文案含「需要授权」关键字（上游权限桥识别协议）；
3. 用户确认 → 整个 turbo 执行期内**不再逐工具审批**（内部 bash / 读写文件 / 网络组等直接放行）；
4. 内层 executor 的 `before_tool_call` rail 链仍执行（补发 chat.tool_call / chat.tool_update 事件，[[S_08]]），但不承担审批职责；
5. 例外：ask_user 的 HITL 交互经 SkillTurboAskUserRail 转 AbortError 中断（[[S_06]]），不走审批面。

审批模型成立的前提是不变量 10/12：内层不得私挂权限 rail（否则出现双重审批/审批遗漏），审批清单不得落后于实际工具面（否则授权范围失真）。

### P6 fallback 子代理权限挂起（interface_deep.spawn_fallback）

节点 fallback 派生子代理时：`_suspend_subagent_permission_rails` 对 PermissionInterruptRail / SkillAuthorizationPermissionRail / SkillTurboPermissionRail 三类 rail 置 `_static_config.enabled=False` + 临时替换 `host.get_permissions_snapshot` 回调 → 子代理阻塞执行 → **finally 恢复**（enabled 复位 + snapshot 回调还原）。子代理以未解决 HITL interrupt 结束时显式 RuntimeError（不吞成空输出，[[S_07]]）。

### P7 AbortError 穿透（静态 + 运行期互补）

静态层：require_abort_reraise 在注册期拒绝无守护的宽泛 except（P1/P2）。运行期三道穿透兜底（行为细节归 [[S_04]]）：`PlanNode.run/run_stream` 显式 `except AbortError: raise` 先于宽泛 except；`execute_subplan*` AbortError 分支不触发 after 直接上抛；`use_tool` 捕获 AbortError 先 `save_resume_ctx` 再上抛。工具层 `call_tool_with_retry` / `run_bash` 对 AbortError 一律不重试直接透传。

## 异常分支与边界 case

| # | case | 行为 / 风险 | 测试覆盖 |
|---|---|---|---|
| 1 | validator 拒绝（注册期） | 用户视角"无匹配skill"静默降级 DeepAgent；排查只能靠服务端 WARNING 日志（skill/file/errors 明细） | test_environment_external_turbo + 技能仓注册门禁（仓外） |
| 2 | strict_builtin_skill_validation | 默认 False（过渡期，失败仅跳过）；True 时校验失败 ValueError 阻止整个启动 | 无（strict=True 专测缺） |
| 3 | 语法错误 | `ast.parse` 抛 SyntaxError → validate 返回 `["语法错误: ..."]`，不抛异常、与其它违规同路处理 | 无（语法错误分支） |
| 4 | 无显式 root 变量 | 0 个 PlanNode 实例 → ValueError；恰好 1 个 → 脆弱兜底采用（依赖 dict 迭代顺序）；>1 个 → PlanCodeValidationError | test_executor_plan_code_import（三分支用例） |
| 5 | 执行期校验失败（流式） | chat.error + complete + return，不抛 NotHandled；外层 LLM 拿不到降级信号，可能误判工具成功 | test_exit_path_frames（E5/E6 帧序列） |
| 6 | fromlist 绕过尝试 | `from skill_codes.xxx import __import__` / `getattr` / `type` 等 → `_DENIED_FROMLIST_NAMES` 拦截（15 项） | 无 |
| 7 | type 三参调用 | builtin / generated profile deny_type_three_args 拒绝动态类型创建；plan_code profile 不启此开关 | 无 |
| 8 | dunder 访问 | deny_dunder_attribute 默认 True；builtin 仅 `__init__` / `__name__` 放行；`__class__` / `__subclasses__` 等全禁 | 部分：test_validator_external_profile（白/黑名单面） |
| 9 | 文件 IO / 网络直连 | pathlib 读写方法在 denied_attribute_call_names 封死；http./urllib./requests. 前缀黑名单 + 白名单缺省拒绝；一律走 use_tool 工具通道 | 部分：test_validator_external_profile（denied 面同上） |
| 10 | `asyncio.gather(return_exceptions=True)` 吞 AbortError | 静态校验无法覆盖（gather 不是 try/except）；技能侧约定显式重抛 + P7 运行期三道穿透兜底 | 部分：运行期穿透由 test_abort_error_guards 锁定；静态面无法覆盖（spec 自认） |
| 11 | 权限 rail suspend 漏恢复 | 父子共享 rail 实例；finally 缺失或 snapshot 回调未替换 → resolve_interrupt 用旧 snapshot 覆盖 → 父 agent 权限状态被污染 | 部分：test_spawn_fallback_suspend_and_interrupt（单元级）；端到端无 |
| 12 | 清洗碰撞 | 不同 skill_name 清洗后同动态包名 → ensure_turbo_package 拒绝后注册者 → 跳过注册（防 import 解析到错误代码，[[S_05]]） | test_turbo_package_loader |
| 13 | 并发请求共享模块级 root 单例 | 不 deepcopy 即绑回调 → 后启动请求覆盖先启动请求回调 → 节点产物写错 executor 的 holder | 部分：test_executor_plan_code_import（deepcopy 独立副本断言） |
| 14 | generated profile 无调用方 | 当前预留、行为冻结；启用前必须重审 deny 清单与调用方约束 | N/A（无调用方，行为冻结） |
| 15 | 白名单前缀过宽 | allowed_import_prefixes 命中过宽（如误含引擎内部包）→ 沙箱面扩大；前缀列表来源必须经 skill_code_import_prefixes 单一通道 | test_validator_external_profile（engine_internal_still_denied） |
| 16 | 审批清单与实际工具面漂移 | 新增 turbo 工具类别未同步 SKILL_TURBO_APPROVAL_TOOLS → 审批卡误导用户；回归测试守护 | test_skill_turbo_approval_tools |
| 17 | 白名单含 plan_node 全路径但禁直连其它引擎模块 | `jiuwenswarm.server.runtime.skill_turbo.plan_node` 是 AbortError/FallbackContractError 唯一合法触点；openjiuwen / fallback_handler 等其余引擎路径均不在白名单（防绕过门面直连内部实现） | test_validator_external_profile（facade 白名单） |
| 18 | 审批文案改写 | 「需要授权」关键字被移除 / 改写 → 上游权限桥无法识别审批卡 → 审批链断裂（文案即协议，改文案必须同步上层识别逻辑） | 无（「需要授权」文案协议） |
| 19 | strict 模式启动失败 | strict_builtin_skill_validation=True 时任一内置技能校验失败 → ValueError 阻止整个启动（适用于严格部署，默认过渡期 False） | 无（同 #2） |

## 修改检查单

以下每条均为"改 X → 必查 Y"的硬性检查项，提交前逐条对照：

- **改白名单/黑名单（任一 profile）** → 必跑注册门禁测试（技能仓 test_turbo_registration_gate 模式：全部 .py 过检 + 入口存在 + 已知违规样本必须检出）；逐一评估绕过面（黑名单优先序、前缀匹配边界、fromlist 路径）；同步本 spec 不变量 3 的数量契约。
- **新增门面符号（skill_turbo_runtime）** → 必同步 builtin profile `allowed_import_exact` 白名单 + 递增 `CONTRACT_VERSION`（[[S_05]]）+ identity 测试。
- **改审批清单 SKILL_TURBO_APPROVAL_TOOLS** → 必同步 tools_loader 分组与实际工具面（[[S_02]]）+ 跑 `test_skill_turbo_approval_tools.py`；文案保留「需要授权」关键字。
- **改 _safe_import** → 必查 fromlist 黑名单完整性（新增危险内建同步进 `_DENIED_FROMLIST_NAMES`）+ 前缀白名单来源仍为 skill_code_import_prefixes 单一通道。
- **改 _SAFE_BUILTINS** → 逐项评估新增内建的逃逸面；能取属性 / 能执行代码 / 能建类型的函数禁入。
- **改 _check_try_reraise 守护识别** → 必同步技能仓 TURBO_CODE_SPEC 的 except 写法约定与现有技能代码模式（裸名 / X.AbortError 两种形态都要识别）。
- **改 AbortError 触点** → 保持 plan_node / runtime 门面单一 re-export 来源，禁止任何环节 import 后再包装；跑 identity 测试（test_runtime_facade.py）。
- **改权限 rail suspend/restore** → 必验证 finally 对称恢复 + snapshot 回调同步替换（构造"子代理中断后父 agent 再审批"用例）。
- **新增护栏代码** → 必须有调用点且有测试证明被走到（护栏不可达 = 假安全）；护栏自身异常路径按 fail-closed 处理（中断而非吞掉）。
- **翻转 deny_relative_import 或收窄目录全量校验** → 先确认相对导入目标有等价的静态校验能力，否则等于打开逃逸面（不变量 9 的机制前提被破坏）。
- **改注册期失败处理（WARNING/strict）** → 同步排查指南：用户唯一可见症状是"无匹配skill"，日志文案必须含 skill/file/errors 三要素。
- **改审批文案** → 保留「需要授权」关键字与工具清单结构；文案变更须与 上游权限桥的识别逻辑同批验证。
- **翻转 strict_builtin_skill_validation 默认值** → 必须先全量跑所有内置/外部技能仓的注册门禁测试，否则启动期 ValueError 直接阻断服务。

## 与其它 spec 的关系

| Spec | 关系 |
|---|---|
| [[S_01]] | 本 spec 的执行期校验/沙箱发生在 S_01 主链路 `_prepare_root_node` 步骤；流式校验失败的 error/complete 语义分叉由 S_01 E5/E6 承接。 |
| [[S_02]] | 注册扫描的调用时序与缓存归 S_02；本 spec 定义被调用的校验规则与失败后果；审批清单与工具分组互为同步对象。 |
| [[S_04]] | require_abort_reraise 的静态强制与本 spec P7 互补，运行期三道穿透的行为细节归 S_04；PlanNode 11 条契约中的异常约定（不吞异常、失败直接 raise）是本 spec 校验规则的语义前提。 |
| [[S_05]] | 相对导入安全性依赖动态包受控 `__path__`；新增门面符号须同步白名单并递增 CONTRACT_VERSION；清洗碰撞拒绝语义归 S_05。 |
| [[S_06]] | AbortError 是 HITL 中断的唯一载体（类型同一性由本 spec 守护）；use_tool 捕获 AbortError 后的断点保存/恢复语义归 S_06。 |
| [[S_07]] | fail-closed 判定（validate_fallback_success 异常同样终止降级）、fallback 子代理权限挂起（本 spec P6）与降级体系（S_07）在 spawn_fallback 处交汇。 |
| [[S_08]] | 内层 before_tool_call 仍补发工具事件（不承担审批）；事件协议归 S_08。 |
| [[S_09]] | SkillTurboPermissionRail 的挂载（interrupt_helpers 替换 rail 类）、config `skill_turbo: ask` 装配归 S_09；本 spec 定义审批语义与文案协议。 |
