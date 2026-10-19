# SkillTurbo 动态包与门面规约（dynamic-package-and-facade）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_05 / dynamic-package-and-facade |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/`（主战场：turbo_package_loader.py / runtime/__init__.py / runtime/tool_utils.py / skill_meta.py 发现侧；environment.py 注册调用侧） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无（首批 specs，由现状代码逆向固化） |

> 代码定位约定：本文一律用"文件 + 方法名"定位；随附行号必然随代码演进漂移，以方法名为准。

## 范围与边界

### 本规约管什么

外部 turbo code 的**动态包生命周期**、`skill_turbo_runtime` **门面**、`tool_utils` **工具契约**：

- 双仓架构（技能侧仓源码权威 ↔ jiuwenswarm 引擎侧）与"约定即协议"的目录约定。
- 发现协议内部：`discover_external_turbo_skills` 的 roots 解析与枚举规则。
- 包名派生（`sanitize_skill_name` / `dynamic_package_prefix`）与 sys.modules 内存注册（`ensure_turbo_package` 七步序列、碰撞检测、rebind、清理、注册态四结局）。
- 门面 `skill_turbo_runtime`：符号表、类型同一性铁律、存在理由、`ensure_runtime_facade` 幂等注册。
- `CONTRACT_VERSION` 机制。
- plan_code 持久化与 resume 自愈（`ensure_packages_for_plan_code`）。
- tool_utils 公开符号全表（21 个函数 + 2 个类，共 23 个）与**工具失败双轨契约**。

### 本规约不管什么

| 子主题 | 归属 spec |
|---|---|
| 三条注册路径顺序 / 外部覆盖内置 / 两条扫描缓存 / planner 路由 / meta.json 字段规范 | [[S_02]] |
| validator 三 profile 规则全集 / 沙箱 namespace / 相对导入静态检查 | [[S_03]] |
| PlanNode 节点契约 / 12 回调 / run 模板方法 / LLM 调用契约 | [[S_04]] |
| HITL 状态机（tcid 机制、save_resume_ctx 四元组、恢复分流） | [[S_06]] |
| 降级链路全景（PlanCodeLoadError 之后的 4 层降级决策） | [[S_07]] |
| 事件流协议（工具事件的 rail 补发） | [[S_08]] |
| `JIUWENSWARM_SHARED_SKILLS_DIRS` 的外层注入（部署侧 / sidecar） | [[S_09]] |

## 不变量

任意时刻，动态包与门面机制必须满足下列 20 条断言：

1. **N1 双仓互不感知**：技能 code 自包含——零 `jiuwenswarm.*` import，一切引擎能力经 `skill_turbo_runtime` 门面（validator 白名单强制，[[S_03]]）；引擎零技能名硬编码——发现、注册、校验、执行全部协议驱动。
2. **N2 前缀单一来源**：`dynamic_package_prefix(skill_name)`（返回 `skill_turbo_codes_{skill}.`）是动态包前缀**唯一来源**；validator 白名单构造、environment 前缀组装等消费方一律经它取值，禁止手写 `skill_turbo_codes_` 字面量（防规则漂移）。
3. **N3 清洗确定性**：`sanitize_skill_name` 把 `\W → _`；空或数字开头补前导 `_`；包名 = `skill_turbo_codes_{sanitized}`。
4. **N4 不进 sys.path**：动态包仅以 `types.ModuleType` + `__path__` 锚点注册进 sys.modules；不污染全局导入路径，多技能不撞名；子模块由 Python 常规 import 机制按 `__path__` 解析。相对导入只能在受控 `__path__`（turbo_codes 目录）内解析——物理上无法逃逸到引擎或其他技能目录。
5. **N5 碰撞拒绝后注册者**：不同 skill_name 清洗后同包名 → `ensure_turbo_package` 返回 None 拒绝注册 → 调用方跳过该 skill（防先注册技能的 import 解析到错误代码）。
6. **N6 注册幂等 + rebind 清理**：目录未变且 sys.modules 已有且 `__path__` 命中 → 复用；目录变更 → WARNING rebind + `_cleanup_package_modules` 清 pkg 及全部 `pkg.*` 子模块后重注册（防陈旧残留）。
7. **N7 注册表互斥**：`_REGISTRY_LOCK`（threading.Lock）串行化全部注册/查询/写入；两张进程级表 `_PACKAGE_REGISTRY`（包名→绝对路径）+ `_PACKAGE_SKILL_NAMES`（包名→原始 skill_name）。
8. **N8 门面只 re-export**：4 个契约类（AbortError / DisableThinkingMixin / FallbackContractError / PlanNode）是 plan_node 的纯 re-export，同一类对象；任何环节重新定义/包装都使 `isinstance` / `except` 失效。
9. **N9 AbortError 同一性链条**：`openjiuwen.core.runner.callback.AbortError` → plan_node re-export → 门面 re-export → skill_code `from skill_turbo_runtime import AbortError`；断链即 HITL 中断被吞。测试守护 test_runtime_facade.py。
10. **N10 FallbackContractError 同一性**：定义在 fallback_handler.py → plan_node re-export → 门面 re-export；同一对象（测试守护同上）。
11. **N11 门面幂等不覆盖**：`ensure_runtime_facade` 已注册则跳过（进程内首个注册者胜出，均为本模块）；重复调用不抛错、不换对象。
12. **N12 门面先于 import**：任何 turbo code import 之前门面必须可解析——三个调用点：environment `__init__`（装配期）、`ensure_turbo_package` 第 1 步、`ensure_packages_for_plan_code` 入口。
13. **N13 CONTRACT_VERSION 递增约定**：新增/删除门面符号属契约变更，应递增 `CONTRACT_VERSION` 并同步 [[S_03]] 白名单（当前无代码强制，约定先行）。
14. **N14 自愈遍历全部 from-import**：`ensure_packages_for_plan_code` 以 MULTILINE 正则遍历 plan_code 全部 from-import 的顶层包（防多行 plan_code 首个非动态包 import 时遗漏）；仍未命中不强行处理，交 exec 自然抛错走降级（[[S_07]]）。
15. **N15 工具失败双轨兼容**：所有下游消费必须同时处理抛异常轨与显式 `success=False` 轨（`_is_explicit_failure` + except 双判定）；禁止 `str(ToolOutput)` 兜底进业务流。
16. **N16 AbortError / CancelledError 永不重试**：`call_tool_with_retry` / `run_bash` 对 AbortError 一律透传；CancelledError 是 BaseException，不被 `except Exception` 捕获自然穿透。
17. **N17 单进程假设**：注册表进程级；当前 per-user sidecar 部署（每用户独立进程）下 rebind 安全；多租户共享进程需改 per-tenant 隔离注册表或 rebind 分支拒绝降级（文件头注释明示的路线图）。
18. **N18 依赖源头直取**：技能 code 内 `from .A import X` 的 X 必须在 A 中有定义（工具符号一律从 tool_utils 源头 import）——门面是 sys.modules 内存注册、磁盘不存在，静态分析器无法穿透 re-export 链。
19. **N19 注册态双查**：`is_package_registered(pkg)` 必须同时命中 `_PACKAGE_REGISTRY` 与 sys.modules 才算已注册——防"表里有但模块对象被外部清掉"的假阳性（自愈判定据此触发）。
20. **N20 门面注册委托别名**：`turbo_package_loader.ensure_runtime_facade` 是对 `runtime.ensure_runtime_facade` 的延迟 import 委托（函数内 import，防模块级循环依赖与外层作用域重定义）；两个名字同一行为，新增调用点任选其一，不得出现第三份实现。

## 接口契约

### 双仓架构

```
技能侧仓 skills sidecar（源码权威）              jiuwenswarm 仓（引擎侧）
<shared_skills_root>/{skill}/                    server/runtime/skill_turbo/
  turbo/                                          runtime/              ← 门面（唯一接口边界）
    meta.json        ← 路由元数据（[[S_02]]）       __init__.py           ← re-export 契约符号（sys.modules 注册为 skill_turbo_runtime）
    turbo_codes/{skill_name}/                     tool_utils.py         ← 共享工具单副本（引擎侧维护）
      {skill_name}_gen_root.py ← 入口 root         plan_node.py          ← PlanNode + AbortError/FallbackContractError re-export
      ...（节点/共享模块，包内相对导入）              turbo_package_loader.py ← 发现 / 动态注册 / resume 自愈
    tests/           ← 注册门禁 + 回归测试           validator.py          ← 静态校验（[[S_03]]）
```

数据流与边界语义：

- 技能目录经 `JIUWENSWARM_SHARED_SKILLS_DIRS`（部署侧 / sidecar 注入，[[S_09]]）到达运行时：`resolve_agent_registered_skill_dirs()`（jiuwenswarm/common/utils.py）解析为 roots，顺序 = **请求级绑定目录 > 共享目录 > 工作区**；失败回退空列表。
- 技能 code 的 import 只有三类：标准库白名单子集（[[S_03]]）→ 门面（`skill_turbo_runtime` / `skill_turbo_runtime.tool_utils`）→ 包内相对导入；执行期这三类分别由 validator 白名单、门面 sys.modules 注册、受控 `__path__` 三种机制保障。
- 引擎对技能实例的文档级引用（如 plan_node.py 契约第 11 条点名 ppt 参考实现）不是代码依赖；引擎代码中不存在任何技能名字面量分支。
- 引擎仓在技能侧仓有 vendored 副本供技能门禁测试引用（test_turbo_registration_gate 经 `vendor/jiuwenclaw` import 引擎 validator）——测试期引用，非运行期依赖。

### 发现协议（turbo_package_loader.discover_external_turbo_skills）

纯发现（不注册包、不校验代码），五步：

1. **roots 解析**：`skill_roots` 缺省 → `resolve_agent_registered_skill_dirs()`（顺序见上）；解析失败 WARNING + 空列表。
2. **技能目录候选枚举**：每个 root 经 `_iter_skill_dir_candidates` 枚举——**root 本身**（`root/turbo` 是目录时）+ **一层子目录**（`child/turbo` 是目录者，sorted）。即 root 可以是"技能目录的父目录"（共享目录形态）或"技能目录本身"。
3. **skill_name 枚举**：`turbo/turbo_codes/` 下每个子目录 = skill_name——跳过 `_` / `.` 开头（`__pycache__` 等）；`find_skill_root_file`（skill_meta.py，5 级优先级链：`{skill_name}_gen_root.py` → `{skill_name}_root.py` → 任意 `*_gen_root.py`（sorted）→ 任意 `*_root.py`（sorted）→ `plan_code.py`；规范与三消费方收口归 [[S_02]]）必须命中，否则跳过。
4. **多源去重**：同 skill_name 多源时按 roots 顺序**取第一个**（seen 集合去重），后续源静默忽略。
5. **产出**：`ExternalTurboSkill(skill_name, external_name, turbo_codes_dir, meta)`；meta 来自 `load_skill_meta`（external_name 优先 meta.json 值，缺省用技能目录名；字段规范归 [[S_02]]）。

约定即协议：`{skill_root}/{external_name}/turbo/turbo_codes/{skill_name}/` 存在入口文件即视为加速面——无技能清单、无注册表硬编码。新增技能零引擎改动。

**发现—注册职责分层**（三层各自独立，边界清晰）：

| 层 | 函数 | 职责 | 副作用 |
|---|---|---|---|
| 发现 | `discover_external_turbo_skills` | 磁盘 → ExternalTurboSkill 列表 | 无（纯读） |
| 包注册 | `ensure_turbo_package` | turbo_codes 目录 → sys.modules 动态包 | sys.modules / 两张注册表 |
| skill 注册 | `environment.register_skill` | ExternalTurboSkill → Skill 记录（[[S_02]]） | environment 技能表 |

发现不注册、注册不发现（ensure_turbo_package 不扫盘）；resume 自愈把两者串起来（发现 → 逐个 ensure）。校验（`_validate_skill_code_dir`）在包注册之前、由 environment 编排——校验失败连包都不注册。

### 包名派生与动态注册（ensure_turbo_package 七步）

```
ensure_turbo_package(skill_name, turbo_codes_dir) -> 包名 | None
  1. ensure_runtime_facade()                     # 门面先就位（N12）
  2. dir_str = Path(turbo_codes_dir).resolve()
  3. with _REGISTRY_LOCK:                        # N7
     4. 清洗碰撞检测：_PACKAGE_SKILL_NAMES[pkg] 已被不同 skill_name 占用
        （如 "p.pt" 与 "p_pt" 清洗后同为 skill_turbo_codes_p_pt）
        → logger.error + return None             # N5
     5. 幂等复用：目录未变 + sys.modules 已有 + __path__ 含 dir_str → 返回包名
     6. 目录变更（同名不同目录）→ WARNING rebind + _cleanup_package_modules(pkg)
        （清 pkg 及全部 pkg.* 子模块）
     7. types.ModuleType(pkg_name) + __path__=[dir_str] + __package__
        → sys.modules[pkg_name] 注册 + 写 _PACKAGE_REGISTRY / _PACKAGE_SKILL_NAMES
```

**注册态四结局**：

| 结局 | 触发条件 | 返回 | 调用方动作 |
|---|---|---|---|
| 首次注册 | 包名未被占用 | 包名 | 继续注册 skill |
| 幂等复用 | 目录未变且 sys.modules 命中 | 包名 | 继续注册 skill（缓存命中路径每次重做，[[S_02]] N8） |
| 碰撞拒绝 | 包名被不同 skill_name 占用 | None | **跳过该 skill 注册**（error 日志） |
| rebind 重注册 | 同 skill_name 不同目录 | 包名 | WARNING 日志；先注册者代码被清换 |

调用方（environment `_scan_external_turbo_skills`，注册顺序与缓存归 [[S_02]]）拿到 None 必须跳过该 skill 注册。

**单进程假设与多租户路线图**（turbo_package_loader.py 文件头注释）：当前 per-user sidecar 每用户独立进程，进程级注册表安全；多租户共享进程下 rebind 会导致先注册租户的 plan_code import 解析到后注册租户的代码——需改为 per-tenant 隔离注册表（`dict[tenant_id, dict[pkg_name, ...]]`）或 rebind 分支拒绝降级（return None）。

### 门面 skill_turbo_runtime（runtime/__init__.py）

公开符号共 7 个：`__all__` 6 项 + `ensure_runtime_facade()`：

| 符号 | 形态 | 语义 |
|---|---|---|
| `AbortError` | re-export（plan_node） | HITL 中断信号（N9） |
| `DisableThinkingMixin` | re-export（plan_node） | thinking 强制 off 的 opt-in Mixin（[[S_04]]） |
| `FallbackContractError` | re-export（plan_node ← fallback_handler） | 终止性降级信号（N10） |
| `PlanNode` | re-export（plan_node） | 节点基类（[[S_04]]） |
| `tool_utils` | 子模块 | 共享工具单副本（本 spec 符号全表） |
| `CONTRACT_VERSION` | int = 1 | 契约符号表版本（见下节） |
| `ensure_runtime_facade` | 函数（不在 `__all__`） | 引擎侧注册入口（N11/N12） |

**类型同一性铁律**——re-export 链条（任一环节断链即失效）：

```
AbortError:           openjiuwen.core.runner.callback.AbortError
                        └─ plan_node.py re-export（注释：整个 skill_turbo 与所有 skill_code
                             一律从本模块取 AbortError，禁止再 from openjiuwen... import）
                             └─ runtime/__init__.py re-export
                                  └─ skill_code: from skill_turbo_runtime import AbortError

FallbackContractError: fallback_handler.py 定义
                        └─ plan_node.py re-export ── runtime/__init__.py re-export ── skill_code
```

任何一环变成"重新定义/包装"都会使 `isinstance` / `except` 判定失效——技能侧 `except AbortError` 守卫（[[S_04]] require_abort_reraise）捕获不到 rail 抛出的中断，HITL 被吞、resume 断链。测试守护：tests/unit_tests/server/runtime/skill_turbo/test_runtime_facade.py——4 组 identity 断言（`runtime.X is plan_node.X`）+ FallbackContractError 与 fallback_handler 定义同一对象 + tool_utils 符号完整性 + 门面注册幂等不换对象。

门面存在的三个理由：

1. **磁盘上不存在 `skill_turbo_runtime` 包**：外部 turbo code 物理位于技能目录，import 一个磁盘不存在的顶层名，IDE / mypy / pylint 无法解析；以中立短名 + `ensure_runtime_facade()`（`sys.modules["skill_turbo_runtime"] = import_module("jiuwenswarm.server.runtime.skill_turbo.runtime")`）运行时内存注册才能工作。
2. **引擎内部重构隔离**：validator 白名单只放行 `skill_turbo_runtime` / `skill_turbo_runtime.tool_utils` 两个稳定名（[[S_03]]）；skill_code 禁止绕过门面引用 `jiuwenswarm.server.runtime.skill_turbo.*` 内部模块（plan_node 除外）——引擎内部模块移动/改名不影响已发布技能。
3. **契约符号触点唯一化**：AbortError / FallbackContractError 的合法触点收敛到 plan_node + 门面（N9/N10），全链路单一类对象。

`ensure_runtime_facade` 幂等注册（N11）：已注册则跳过，不覆盖已注册对象；三个调用点保证任何 code import 之前门面可解析（N12）——environment `__init__`（装配期，[[S_02]]）、`ensure_turbo_package` 第 1 步、`ensure_packages_for_plan_code` 入口。

**依赖源头直取规范**（N18）：技能 code 的 import 形态固定为——标准库 → 门面 → 门面工具 → 包内相对导入，段内字母序（validator 静态检查合规，[[S_03]]）。`from .A import X` 的 X 必须在 A 有定义；工具符号一律从 tool_utils 源头 import，不得经门面或其它模块再转手——违反则静态分析全链路报不可解析。技能侧 import 范式（全 codebase 统一）：

```python
from skill_turbo_runtime import AbortError, DisableThinkingMixin, FallbackContractError, PlanNode
from skill_turbo_runtime.tool_utils import (
    BashExecError, BashResult, combined_output, quote_path, run_bash,
    strip_line_numbers, parse_bash_payload, normalize_tool_text, get_tool_file_list,
)
from .ppt_common import PptCommon          # 包内相对导入
```

### CONTRACT_VERSION 机制

- 定义：`runtime/__init__.py` `CONTRACT_VERSION = 1`（int，随 `__all__` 导出）。
- 语义：门面向外部 turbo code 暴露的**契约符号表版本**——新增/删除门面符号属契约变更，应递增版本，供外部技能（或未来的加载器）检测不兼容。
- 现状：仓库内无强校验消费方（仅定义 + 单测 `isinstance(int)` 断言），属"约定先行"机制。

| 变更类型 | CONTRACT_VERSION | 附带动作 |
|---|---|---|
| 新增门面符号 | **递增** | 同步 [[S_03]] 白名单 + test_runtime_facade.py 符号清单 + 技能侧文档（TURBO_CODE_SPEC） |
| 删除/改名门面符号 | **递增**（破坏性） | 同上 + 全量技能 import 面排查 |
| 纯语义调整（签名/行为变化） | 不动版本 | 消费方契约文档同步 |
| tool_utils 新增符号 | 不动版本 | 符号全表 + 完整性测试同步 |

### plan_code 持久化与 resume 自愈

- **持久化**：HITL 中断时 `executor.use_tool` 捕获 AbortError → `save_resume_ctx(session, plan_code, inputs, pending_tool_call_id, task_states)`（[[S_06]]）——plan_code 以字符串形态随断点落库（session state → checkpointer）；executor 亦持 `self._current_plan_code`。
- **自愈** `ensure_packages_for_plan_code(plan_code)` 三步：

```
1. for m in _PLAN_CODE_IMPORT_RE.finditer(plan_code):     # MULTILINE 全量遍历（N14）
     top = m.group(1).split(".")[0]
     top 以动态包前缀开头 且 not is_package_registered(top) → 加入 unregistered
2. unregistered 非空 → discover_external_turbo_skills() 全量重发现
   → 逐个 ensure_turbo_package → 全部命中即返回
3. 仍未命中 → 不强行处理：交由后续 exec(plan_code) 的 import 自然抛错
   → PlanCodeLoadError → 既有降级链路兜底（[[S_07]]）
```

- 调用点：`executor._load_plan_namespace` 每次加载 plan_code（含 resume 重放）前执行，幂等——技能侧对此完全无感知（技能不感知"进程重启后包要重注册"这件事）。

### tool_utils 公开符号全表（21 函数 + 2 类，共 23 个）

runtime/tool_utils.py，引擎侧单副本维护（技能不再各自复制 bash 工具代码）：

| # | 符号 | 语义要点 |
|---|---|---|
| 1 | `strip_line_numbers(content)` | 去除 read_file 返回的 `cat -n` 行号前缀（`^ *\d+\t` MULTILINE） |
| 2 | `strip_code_fence(raw)` | 三级剥离：整段 fence match → search 首个 fence（LLM 前置思考文本场景）→ 裸 HTML 文档提取（`<!DOCTYPE html>...</html>`） |
| 3 | `parse_json_tolerant(text)` | json.loads 失败 → 内嵌引号修复（`_repair_inner_quotes`）重试 → 仍失败抛 JSONDecodeError |
| 4 | `parse_llm_json(raw)` | strip_code_fence + parse_json_tolerant（一步到位的 LLM JSON 解析） |
| 5 | `get_tool_data(result)` | ToolOutput `.data` / dict 本体（语义化 getter 的底层） |
| 6 | `get_tool_success(result)` | `.success` 属性或 `data["success"]`，缺省 False |
| 7 | `get_tool_error(result)` | `.error` 属性或 `data["error"]`，缺省 "" |
| 8 | `get_tool_content(result)` | read_file 文本内容，自动 strip_line_numbers；str 直通 |
| 9 | `get_tool_stdout(result)` | `_extract_bash_result` 优先；回退 `normalize_tool_text` |
| 10 | `get_tool_stderr(result)` | `data["stderr"]`；显式失败无 data 时 `[ERROR]: ...` 归一文本作 stderr |
| 11 | `get_tool_exit_code(result, default=1)` | 结构化提取 + 失败信号反推（四态语义见边界 case 10） |
| 12 | `get_tool_file_list(result)` | list_files 的 files / entries / filenames / items 键链 + basename 归一 |
| 13 | `is_transient_tool_error(error)` | 匹配 `_TRANSIENT_ERROR_PATTERNS`（"Cannot operate on a closed database"——底层 per-file SQLite 锁 use-after-close 竞态） |
| 14 | `call_tool_with_retry(node, tool_name, *, log_prefix, **kwargs)` | 2 次尝试；双轨失败判定 + 瞬态模式 gc 自愈（展开见下） |
| 15 | `normalize_tool_text(result)` | 任意形态归一纯文本：str→本身；`.data` dict→stdout/output/result/content 键链→json.dumps；dict→同+嵌套 data；error 属性→`[ERROR]: {error}` |
| 16 | `parse_bash_payload(text)` | 文本形态解析：`[ERROR]` 前缀→exit1；`Exit code N` 前缀→解析剥前缀；JSON payload→字段提取+coerce；普通文本→exit0+coerce |
| 17 | `parse_bash_result(result)` | 结构化 `_extract_bash_result` 优先（入参已是 BashResult 直通——防 getter 文本兜底恒返 0），文本解析兜底 |
| 18 | `run_bash(node, command, *, timeout_seconds=300, required=True, workdir=None)` | 节点执行 bash 的公共入口（展开见下） |
| 19 | `quote_path(path)` | 双引号包裹 + 内部 `"` 转义（shell 参数） |
| 20 | `combined_output(result)` | stdout + "\n" + stderr 合并 |
| 21 | `extract_path_candidates(text, *, suffixes=())` | 从自由文本提取显式本地路径候选（展开见下） |
| 22 | `BashResult` | frozen dataclass：exit_code / stdout / stderr / raw（含反推修正后的 exit_code） |
| 23 | `BashExecError` | RuntimeError 子类：required=True 时非零退出码抛出；run_bash 全部失败/未注册也抛 |

三个高复杂度函数的展开：

**`call_tool_with_retry`（双轨失败 + 瞬态自愈）**：

```
attempt 1: result = await node.call_tool(tool_name, **kwargs)
  异常轨: raise 非 AbortError 异常 → 瞬态模式（is_transient_tool_error(str(exc))）？
            是 → gc.collect() → attempt 2；否 → 原样 raise
  显式失败轨: _is_explicit_failure(result)（success is False，兼容属性与 dict 键）
            且 is_transient_tool_error(error 文本) → gc.collect() → attempt 2
            非瞬态显式失败 → 原样返回 result（交业务层判定）
attempt 2: 同上；仍失败 → 按各自轨终结（raise / 返回）
永不重试: AbortError（透传）、CancelledError（BaseException 自然穿透）——N16
```

gc 的必要性：底层锁注册表是 WeakValueDictionary，陈旧实例须等 GC 回收后弱条目才消失；主动 `gc.collect()` 毫秒级、仅瞬态路径触发。瞬态错误在节点内自愈，不升级节点失败、不消耗 plan 级 fallback 预算。

**`run_bash`（两段 attempts）**：

```
tool_attempts = [("bash", 带 timeout), ("bash", 不带 timeout)]
  # 部分 bash 工具注册形态不支持 timeout 参数：第一次 ValueError → 降级第二段
for name, kwargs in tool_attempts:
    try: return parse_bash_result(await node.call_tool(name, command=..., **kwargs))
    except BashExecError: raise                      # 直接终结
    except AbortError:  raise                        # N16 透传
    except Exception as e: last_error = e; continue  # 记录后尝试下一段
全部失败 → raise BashExecError("无法执行 bash 命令...") from last_error
工具未注册 → BashExecError("未注册 bash 工具...")
```

实际运行时 bash 工具常只回 `data={"content": ...}` 不带 exit_code/stdout/stderr——退出码由 `_coerce_exit_code` 从失败信号反推、stdout 经 content/output/result 回退链提取（`parse_bash_result` / `normalize_tool_text`）。

**`extract_path_candidates`（URL 防误匹配三规则）**：

1. `_is_windows_drive_start`：盘符前一个字符**不得是字母数字**（排除 `https://` 的 s）且盘符后必须紧跟 `:`+`/`或`\`；
2. `_is_unix_abs_start`：`/` 的前置字符为字母数字或 `/ . : ?` 时跳过（排除 URL 路径段与 `../` 相对路径）；
3. 骨架跳过：`len(candidate) <= 3` 的 `D:\` / `/` 跳过；`_PATH_STOP_CHARS`（空白/引号/中西文括号标点）终止 token；suffixes 大小写不敏感过滤；按出现顺序去重。

漏任一规则即用户 query 中的 URL 被误判为本地路径、传给工具触发网络访问同步阻塞。

**getter 家族选择指南**（按消费场景选函数，禁止混用）：

| 场景 | 用哪个 | 说明 |
|---|---|---|
| 读文件文本 | `get_tool_content` | 自动去行号；str 直通 |
| 判工具成败 | `get_tool_success` + `get_tool_error` | 显式失败轨的入口 |
| bash 执行 | `run_bash` → `BashResult` | 不要裸 call_tool("bash") 后自己拼解析 |
| bash 结果复析 | `parse_bash_result` / `combined_output` | 已有 tool result 时 |
| 退出码判定 | `get_tool_exit_code` | 四态语义（边界 case 10） |
| 目录列举 | `get_tool_file_list` | 多键链 + basename 归一 |
| LLM JSON | `parse_llm_json` | fence + 引号修复一步到位 |
| 诊断文本 | `normalize_tool_text` | 仅日志/展示，**不进业务流判定**（N15） |

### 工具失败双轨契约

- 工具层失败表达**两轨并存**：抛异常轨（工具函数直接 raise）/ 显式失败轨（返回 `success=False` 结构化结果，不抛）。
- 所有下游消费必须双轨兼容：异常轨走 `except`（call_tool_with_retry 的重试判定、run_bash 的 last_error）；显式失败轨走 `_is_explicit_failure`（`success is False`，兼容 ToolOutput 属性与 dict 键）+ error 文本模式匹配（is_transient_tool_error）。
- 禁止 `str(ToolOutput)` 兜底进业务流——文本化丢结构，失败语义不可判定。
- 读超时 / 失败返回与空内容必须可区分（哨兵值歧义是检查项：空串既可能是"无输出"也可能是"读取失败被吞"）。

## 数据结构

### ExternalTurboSkill（frozen dataclass，turbo_package_loader.py）

| 字段 | 类型 | 语义 |
|---|---|---|
| `skill_name` | str | turbo_codes 下的子目录名（如 "ppt"），包名派生输入 |
| `external_name` | str | 技能目录名（如 "pptx-craft"，meta.json external_name 优先） |
| `turbo_codes_dir` | str | turbo_codes 目录绝对路径（动态包 `__path__`） |
| `meta` | dict | turbo/meta.json 内容（路由元数据，规范归 [[S_02]]） |

### 进程级两张注册表

| 表 | 键 → 值 | 用途 |
|---|---|---|
| `_PACKAGE_REGISTRY` | 包名 → turbo_codes 目录绝对路径 | 注册态查询（`is_package_registered` 同时查此表与 sys.modules）、幂等/rebind 判定 |
| `_PACKAGE_SKILL_NAMES` | 包名 → 原始 skill_name（清洗前） | 清洗碰撞检测（N5） |

两者均在 `_REGISTRY_LOCK` 内读写；生命周期 = 进程（N17 单进程假设）。

### Skill 记录关联字段（注册产物，完整 8 字段模型归 [[S_02]]）

- `package_name: str`（如 "skill_turbo_codes_ppt"）：planner 组装 plan_code 时派生模块名 `{package_name}.{skill_name}.{root_stem}`。
- `turbo_codes_dir: str`：外部技能入口定位 `{turbo_codes_dir}/{skill_name}/`。
- 内置注册两者为空串（内置走 `skill_code_import_package` 路径，`environment._build_plan_code`）。

### BashResult / BashExecError（runtime/tool_utils.py）

- `BashResult`：frozen dataclass，`exit_code`（含 `_coerce_exit_code` 反推修正）/ `stdout` / `stderr` / `raw`；`parse_bash_result` 与 `run_bash` 的统一返回。
- `BashExecError`：RuntimeError 子类；`required=True` 时 run_bash 非零退出码抛出；技能侧可捕获后取 `combined_output` 定位。

### _PLAN_CODE_IMPORT_RE

`re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+", re.MULTILINE)`——plan_code 顶层包名提取的唯一正则；MULTILINE 保证多行 plan_code 全量遍历（N14）。

### 动态包对象形态（sys.modules 中的锚点）

| 属性 | 值 | 说明 |
|---|---|---|
| `__name__` | `skill_turbo_codes_{sanitized}` | 与注册表键一致 |
| `__path__` | `[dir_str]`（单元素 list） | 子模块解析锚点；幂等复用判定依据 |
| `__package__` | 包名自身 | 包内相对导入的解析基准 |

包对象**不含任何代码**——只是 `__path__` 锚点；`pkg.sub` 的加载由 Python 常规 import 机制按 `__path__` 扫目录完成。这意味着删除技能目录后 sys.modules 仍可能持有已加载子模块，直到 rebind 清理（N6）——排查"改了代码不生效"先想到 rebind/清理链。

## 执行路径全景（P1-P6）

- **P1 技能到达运行时**：技能侧仓的技能目录 → 部署侧注入 `JIUWENSWARM_SHARED_SKILLS_DIRS`（[[S_09]]）→ `resolve_agent_registered_skill_dirs()` 解析 roots（请求级绑定 > 共享目录 > 工作区）。root 形态两种：共享目录（其子目录是技能目录）或技能目录本身（`_iter_skill_dir_candidates` 两者都兼容）。
- **P2 发现**：`discover_external_turbo_skills`（roots → `_iter_skill_dir_candidates` → `turbo/turbo_codes/{skill_name}` → 入口命中 → `ExternalTurboSkill` 列表）。纯发现：不注册包、不校验代码、无副作用；结果只被 P3 与 P5 自愈消费。
- **P3 注册**：environment `_scan_external_turbo_skills` 四步：
  1. 缓存判定（roots frozenset key + 各 turbo_codes 目录 max .py mtime 快照；命中 → 跳过扫盘+校验，但仍幂等重做包注册与 skill 注册，边界 case 18）；
  2. `_validate_skill_code_dir`（[[S_03]] builtin profile + 该技能专属动态包前缀，`_validator_for_external_skill` 构造；逐 .py 校验，任一失败 → 跳过该 skill）；
  3. `ensure_turbo_package`（七步，四结局；None → 跳过注册）；
  4. `register_skill`（Skill 记录含 package_name / turbo_codes_dir；外部覆盖内置，[[S_02]]）。
- **P4 import 解析**：executor `_load_plan_namespace` → `exec(plan_code)` → `from skill_turbo_codes_{skill}.{skill_name}.{stem} import root` → sys.modules 动态包 `__path__` 解析子模块 → 技能代码内 `from skill_turbo_runtime import ...` 命中门面（N12 先行注册）→ 包内相对导入在受控 `__path__` 内解析（N4，物理隔离）。root 经 deepcopy 后进入执行（[[S_04]] N11）。
- **P5 resume 自愈**：中断时 plan_code 随 resume_ctx 持久化（[[S_06]]）→ 恢复侧（新进程/新请求）`executor._load_plan_namespace` → `ensure_packages_for_plan_code`（未注册包 → 全量重发现 + 逐个 ensure → 仍未命中交 exec 自然报错走降级 [[S_07]]）。技能侧零感知——不感知"进程重启后包要重注册"这件事。
- **P6 工具结果消费**：节点 `call_tool`（[[S_04]]）返回 ToolOutput / dict / 异常 → tool_utils getter 家族与 bash 解析机器（双轨判定，N15）→ `BashResult` / 结构化 dict 进业务流。瞬态失败在 `call_tool_with_retry` 层自愈（gc + 重试 1 次），不外溢。

## 异常分支与边界 case

| # | case（场景 / 现行为 / 风险） | 测试覆盖 |
|---|---|---|
| 1 | **同 skill_name 多源**：roots 顺序取第一个，后续源静默忽略（用户视角不可见，排查靠 roots 顺序）。 | 部分：test_turbo_package_loader（多 roots 发现） |
| 2 | **目录变更 rebind**：WARNING + 清 pkg 及 `pkg.*` 子模块重注册；单进程安全；多租户共享进程不安全（N17，改动 rebind 行为前必读文件头注释）。 | test_turbo_package_loader（rebind 用例） |
| 3 | **清洗碰撞**：`"p.pt"` 与 `"p_pt"` → 同包名 `skill_turbo_codes_p_pt` → 后注册者 error 日志 + return None + 跳过注册。 | test_turbo_package_loader |
| 4 | **skill_name 目录名边界两种形态**：`_` / `.` 开头 → 发现阶段直接跳过（`__pycache__` / `.git` 在 mtime 计算与代码校验中同样排除）；数字开头（如 `1ppt`）目录名合法 → sanitize 补前导 `_` → 包名 `skill_turbo_codes__1ppt`（双下划线）——前者根本不进发现，后者注册成功，两回事不得混淆。 | 无 |
| 5 | **入口未命中**：turbo_codes 目录存在但 `find_skill_root_file` 返回 None → 发现阶段跳过该 skill（不注册、无报错面）。 | test_turbo_package_loader（no_root_file_skipped） |
| 6 | **meta.json 部分缺失**：含任一已知键（external_name / description / match_keywords）即合法——仅含 external_name 也可路由；无文件 / 解析失败 / 不含任何已知键 → `{}` 走默认 description / keywords（[[S_02]]）。 | test_environment_skill_meta |
| 7 | **resume 自愈仍未命中**：交 `exec(plan_code)` 自然抛 ImportError → `PlanCodeLoadError` → 降级链路（不强行构造包、不静默跳过 import——保证失败可见）。 | test_executor_plan_code_import（unresolvable 用例） |
| 8 | **多行 plan_code**：首个 import 是非动态包时正则仍须遍历到后面的动态包 import（MULTILINE，N14）。 | test_turbo_package_loader（multiline 用例） |
| 9 | **bash 工具注册形态不支持 timeout**：第一次 ValueError → 降级为不带 timeout 重试（run_bash 两段 attempts 设计；bash 工具形态变化时必查此处）。 | test_tool_utils_bash_parsing（降级重试用例） |
| 10 | **exit_code 语义四态**：显式 exit_code 采用；无显式但 success=True → 0（非 default，防成功调用被误判失败）；完全无信息 → default=1；exit_code=0 但有失败信号（success=False / error 非空 / `[ERROR]` 标记 / `Exit code N` 文本）→ 反推升 1（bash 偶发只回 `content="Exit code 1\n..."` 不带 exit_code 字段，不识别会把失败当成功）。 | test_tool_utils_bash_parsing（四态用例组） |
| 11 | **`Exit code N` 文本前缀必须识别**：parse_bash_payload / `_coerce_exit_code` 都要消费该形态（convert 类命令失败只体现在 content 文本时的唯一失败信号）。 | test_tool_utils_bash_parsing |
| 12 | **URL 防误匹配**：盘符前字母数字排除、Unix `/` 前置字符排除、`len<=3` 骨架跳过（extract_path_candidates 三规则；漏一条即 URL 被当本地路径传给工具触发网络访问阻塞）。 | test_tool_utils_path_extract |
| 13 | **SQLite use-after-close 瞬态错误**：模式匹配 + `gc.collect()` + 重试 1 次（WeakValueDictionary 锁注册表回收时机问题）；节点内自愈，不升级节点失败、不消耗 plan 级 fallback 预算。 | test_tool_utils_call_with_retry |
| 14 | **内嵌引号修复已知局限**：内嵌引号紧跟 ASCII `,` + 空格 + 引号的罕见场景误判为结构闭引号 → 修复后仍失败则抛出，走既有重试/失败路径（与未修复行为一致，可接受）。 | test_tool_utils_json_repair（局限文档化） |
| 15 | **BashResult 直通**：`_extract_bash_result` 入参已是 BashResult 直接返回——否则 get_tool_exit_code 对 run_bash 结果走文本兜底恒返 0。 | test_tool_utils_bash_parsing（BashResult 直通用例） |
| 16 | **CancelledError 穿透**：BaseException 不被 `except Exception` 捕获，call_tool_with_retry / run_bash 无需显式分支即透传（注释明示，勿加捕获）。 | test_tool_utils_call_with_retry + test_exit_path_frames（E9） |
| 17 | **外部 skill 同名覆盖内置注册**：`register_skill` 统一入口，覆盖打 info 日志（[[S_02]] N1）；覆盖发生在 Skill 记录层，动态包层无覆盖语义（包名碰撞走拒绝，N5）。 | 无（同名覆盖用例空转，见 test_tool_description_external 已知缺陷） |
| 18 | **缓存命中路径跳过校验**：`_external_scan_cache` 命中只跳过扫盘 + 代码校验，`ensure_turbo_package` + `register_skill` 必须幂等重做（[[S_02]] N8）——包注册状态与扫描缓存生命周期不同步。 | 部分：test_environment_external_turbo（cache_hit 用例自认弱断言） |
| 19 | **`is_package_registered` 双查**（N19）：注册表命中但 sys.modules 缺失（如测试中手动清理）→ 判未注册 → 触发自愈重注册；单查一边都会产生假阳性/假阴性。 | 无 |
| 20 | **门面重复注册无害**：`ensure_runtime_facade` 幂等（N11）；多个调用点重复触发是设计内行为，不是缺陷——但不得依赖"谁先调用"的顺序假设。 | test_runtime_facade（幂等用例） |

## 修改检查单

1. 改包名规则（sanitize / 前缀）→ 必查 `dynamic_package_prefix` 全部消费方（environment `_validator_for_external_skill`、executor `_safe_import` 前缀白名单）+ 碰撞检测 + **plan_code 持久化兼容**（存量 resume_ctx 里的 plan_code 引用旧包名，改名 = 存量断点自愈失效）+ 测试夹具（技能侧仓 test_turbo_registration_gate 的 `allowed_import_prefixes=["skill_turbo_codes_ppt."]`）。
2. 改门面符号（增/删/改名）→ 必查四件事：CONTRACT_VERSION 递增（N13）+ test_runtime_facade.py 同一性/完整性断言 + [[S_03]] validator 白名单 + 技能侧 import 面（技能侧仓全量 turbo code 的 `from skill_turbo_runtime ...` 语句）。
3. 改 tool_utils 契约（签名/返回语义）→ 必查横向全部消费点（引擎侧 rails/executor + 技能侧 getter 家族与 run_bash 调用）+ 双轨兼容（N15）+ `test_runtime_facade.py::test_tool_utils_submodule_importable_and_complete` 符号清单断言（该断言是 test_runtime_facade.py 内的测试函数，非独立文件）。
4. 改 extract_path_candidates → 必查 URL / UNC 回归用例（盘符前置字符 / Unix 前置字符 / 骨架跳过 / suffixes 过滤）。
5. bash 工具形态变化（timeout 支持 / 返回字段）→ 必查 run_bash 两段 attempts 与 `_coerce_exit_code` 失败信号集（success/error 标记/`[ERROR]`/`Exit code N` 文本）。
6. 改发现协议（目录约定 / 入口链）→ 必查 `find_skill_root_file` 三消费方（[[S_02]]）+ 技能侧 TURBO_CODE_SPEC 同步 + `_external_scan_cache` 的 mtime 快照 key。
7. 改 `ensure_turbo_package` rebind 行为 → 必查单进程假设注释（N17）+ `_cleanup_package_modules` 子模块清理完整性（漏清即陈旧代码复活）。
8. 改 `ensure_packages_for_plan_code` 正则/流程 → 必查多行 plan_code 回归 + `PlanCodeLoadError` 降级链路（[[S_07]]）+ 调用点幂等性（`_load_plan_namespace` 每次加载前执行）。
9. 新增门面暴露的工具函数 → 必查依赖源头直取（N18：在定义模块有定义、tool_utils 子模块可达）+ [[S_03]] 白名单 + CONTRACT_VERSION 判定表。
10. 改 `ensure_runtime_facade` 调用点 → 必查新增的 code import 路径是否全部被三个调用点（N12）覆盖；新增独立加载入口时必须补门面注册前置。
11. 改 `_TRANSIENT_ERROR_PATTERNS` → 必查瞬态自愈范围双向影响：新增错误模式 = 自愈面扩大（好）但也可能掩盖真实持久失败（需确认该错误确实可由 gc+retry 自愈）；删模式 = 竞态失败升级节点失败。
12. 改 `is_package_registered` 判定 → 必查 N19 双查语义（单边查询会破坏自愈触发条件）。

## 与其它 spec 的关系

| Spec | 关系 |
|---|---|
| [[S_01]] | 执行管线的 P3（Environment 装配）与 P6（plan 加载）消费本 spec 的注册与门面；`_load_plan_namespace` 是 resume 自愈的唯一调用点。 |
| [[S_02]] | 本 spec 的 P2/P3 只描述发现函数内部与包注册机制；三条注册路径顺序、外部覆盖内置、扫描缓存、planner 路由、meta.json 字段规范归 S_02。find_skill_root_file 的规范归 S_02、实现被本 spec 的发现协议消费。 |
| [[S_03]] | validator 白名单消费本 spec 的门面稳定名与 `dynamic_package_prefix`；相对导入安全性依赖本 spec 的受控 `__path__`（N4——物理隔离，非静态检查）。 |
| [[S_04]] | 门面 re-export 的 4 个契约类的节点侧语义（契约 11 条、12 回调、模板方法）归 S_04；tool_utils 是节点工具调用面（call_tool）下游的结果消费契约。 |
| [[S_06]] | plan_code 随 resume_ctx 持久化的写入侧（save_resume_ctx 四元组）与恢复分流归 S_06；本 spec 只负责恢复前的动态包自愈。 |
| [[S_07]] | 本 spec 的碰撞拒绝 / 自愈未命中 / PlanCodeLoadError 都汇入 S_07 的降级链路（用户视角"无匹配 skill"或加速降级标准流）。 |
| [[S_08]] | run_bash / call_tool_with_retry 触发的工具事件（chat.tool_call / tool_update）由 rail 层补发，协议归 S_08。 |
| [[S_09]] | `JIUWENSWARM_SHARED_SKILLS_DIRS` 的注入方（部署侧 / sidecar 形态）与请求级绑定目录的上游语义归 S_09。 |
