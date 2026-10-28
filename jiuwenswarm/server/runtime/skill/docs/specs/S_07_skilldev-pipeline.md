# skilldev 技能自演进流水线（未接线）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_07 / skilldev-pipeline |
| 关联模块 | jiuwenswarm/server/runtime/skill/skilldev/（全部） |
| 最近一次修订日期 | 2026-10-16 |
| 关联 feature | 暂无（初版建档） |

> **工程现状声明（本 spec 的第一读者须知）**：skilldev 当前是**设计完备、代码半成品、完全未接线**的脚手架模块——生产不可达（三重接线断点见"接线状态"节）。本 spec 如实记录现状与设计意图，供后续接线 / 实现工作时校准；**不要假设其中任何 Agent 能力已工作**。

## 范围 / 边界

skilldev 子包：一条**确定性工程流水线**（非对话式 Agent），从用户需求产出可安装的 `.skill` 包——需求 → 规划 → 生成 → 校验 → 测试 → 评测 → 改进 → 打包 → 描述优化。**与 SkillManager 完全解耦**（`deps.py` 设计原则：SkillManager / EvolutionService / 对话历史对 SkillDev 不可见），以 `.skill` zip 为交接物（前端 `skilldev.download` 拉取后经标准 `skills.install` 入口手动安装——**当前无自动安装通道**）。

**例外**：`skilldev/state_utils.py` 是包内的"异类"——skills_state.json 纯函数库，被 SkillManager **反向引用**（历史归置，见 [[S_02]]）；它不属于流水线，也不受本 spec 的"未接线"状态影响。

## 分层架构（DESIGN.md 规定，只允许上层依赖下层）

```
service.py（请求路由，无状态）
  → pipeline.py（状态机编排）
    → stages/*.py（11 个阶段处理器）
      → context.py（阶段执行环境）
        → deps.py（依赖注入）
          → store.py（checkpoint 持久化）
          → workspace.py（任务工作区）
schema.py（全层共享数据模型）
```

## 数据结构 / 状态机

### SkillDevStage（15 值枚举）

```
INIT → PLAN → PLAN_CONFIRM(挂起) → GENERATE ⇄ VALIDATE
  → TEST_DESIGN → TEST_RUN → EVALUATE → REVIEW(挂起)
  → IMPROVE → (iteration+1, 回 TEST_RUN 迭代)
  → PACKAGE → DESC_OPTIMIZE_CONFIRM(挂起) → DESC_OPTIMIZE → COMPLETED
                                                  ↘ ERROR（任一阶段异常终态）
```

10 个执行阶段有 handler（`SkillDevPipeline.STAGE_HANDLERS`），3 个挂起点无 handler（由 `SUSPENSION_POINTS` 注册表处理），2 个终态。三条链：主链（INIT→…→REVIEW）、迭代环（IMPROVE→TEST_RUN→EVALUATE→REVIEW，每轮 `iteration+1`）、收尾链（PACKAGE→…→COMPLETED）。

### SkillDevState（唯一可信源）

字段分组：任务标识（task_id / stage / mode / iteration）、输入（input）、中间产物（reference_texts / existing_skill_md / plan / plan_confirmed_at / evals / eval_results / feedback_history / desc_optimize_result）、输出（zip_path / zip_size）、元数据（created_at / updated_at / error）。序列化三方法：`to_checkpoint_dict` / `from_checkpoint_dict` / `to_status_dict`。

`SkillDevTaskMode` 三值：`CREATE` / `CREATE_WITH_RESOURCES` / `MODIFY`（`determine_task_mode`：existing_skill→MODIFY；resources→CREATE_WITH_RESOURCES；否则 CREATE）。

### 挂起点机制（声明式，SUSPENSION_POINTS 注册表）

| 挂起点 | confirm_type | actions | on_resume | next_stage |
|---|---|---|---|---|
| `PLAN_CONFIRM` | plan_confirm | confirm / modify | data.plan 可覆写 state.plan，记 plan_confirmed_at | 静态 GENERATE |
| `REVIEW` | review | accept / improve | feedback_history.append | **函数**：improve→IMPROVE，否则→PACKAGE |
| `DESC_OPTIMIZE_CONFIRM` | desc_optimize_confirm | optimize / skip | 无状态写入 | **函数**：optimize→DESC_OPTIMIZE，否则→COMPLETED |

Pipeline 命中挂起点：emit `TODOS_UPDATE` → emit `CONFIRM_REQUEST` → checkpoint → break。恢复：校验确为挂起点 → `on_resume(state, data)` → `next_stage`（可为 callable）→ 续跑。**单一 `skilldev.respond` 统一确认入口**——新增挂起点只改注册表，前端零改动。

### 事件系统（11 种 SkillDevEventType）

流程控制（STAGE_CHANGED / PROGRESS / ERROR）、对话流（AGENT_THINKING / TEST_PROGRESS）、结构化 UI 驱动（CONFIRM_REQUEST / TODOS_UPDATE / ARTIFACT_READY）、数据载体（EVAL_READY / VALIDATE_RESULT / DESC_OPT_READY）。设计原则："后端推的每个事件，前端都应能直接映射到一个 UI 动作"。事件经 `asyncio.Queue` 缓冲，Service 转为 `AgentResponseChunk`；流收尾帧由 Service 包装（`skilldev.started / suspended / completed / error`）。

### 持久化与工作区

- `StateStore`：`~/.jiuwenswarm/agent/jiuwenclaw_workspace/skilldev/{task_id}/state.json`（**不加锁**——注释声明"路由层保证同一 task_id 路由到同一实例"；**无原子写**，见风险清单）；checkpoint 时机三处（阶段完成跳转后 / 挂起暂停前 / 异常转 ERROR 后）。
- `WorkspaceProvider`（**与 workspace_provider.py 的 `SkillWorkspaceProvider` 同名不同类**）：任务目录下标准四目录 `resources/`（上传资源）/ `skill/`（生成区）/ `evals/`（evals.json + iteration-{N}/）/ `output/`（最终 .skill 包）。`sync_to_remote` 空操作（扩展点）。

## 各阶段实现状态（截至建档日）

| 阶段 | 状态 | 说明 |
|---|---|---|
| INIT | 半实现 | 资源解析仅 .txt/.md；已有 skill zip 解压返回 None |
| PLAN | 占位 | `_generate_plan` 返回硬编码占位；JSON 容错解析已实现 |
| GENERATE | 占位 | 写 `# {filepath}` 占位文件；生成顺序解析（SKILL.md 优先→scripts/→其余）已实现 |
| VALIDATE | **已完整** | frontmatter 校验（name/description 必填、key 白名单、name kebab-case ≤64、desc 无尖括号 ≤1024）；失败**回退 GENERATE**（无重试上限，见风险） |
| TEST_DESIGN | 占位 | 返回 1 条占位用例；对齐官方 evals.json schema |
| TEST_RUN | 占位 | 设计为 with_skill/baseline 双子 Agent 对照并行 |
| EVALUATE | 聚合已实现 | per-config mean/stddev/min/max + delta 聚合与 benchmark.md 渲染完整；Grader/Analyst 占位 |
| IMPROVE | 占位 | 六大改进哲学 prompt 已内化 |
| PACKAGE | **已完整** | zip 打包 + 排除规则（`__pycache__` / node_modules / .git / evals / *.pyc 等） |
| DESC_OPTIMIZE | 框架完整 | 60/40 分层切分（seed=42）+ 优化循环（MAX_ITERATIONS=5，选 test 分最高防过拟合）+ 写回 frontmatter；仅三处模型调用占位 |

评测设计**对齐 Anthropic 官方 skill-creator**（官方走 `claude -p` CLI，本实现改走自有模型 API）。

## 接线状态（三重断点，生产不可达）

1. `common/schema/message.py` 的 `ReqMethod` **没有定义任何 `SKILLDEV_*` 枚举成员** → `service.py` 模块级 `_METHOD_DISPATCH` 在 import 时即抛 `AttributeError`。
2. `skilldev/__init__.py` 只导出 state_utils 的 10 个函数，**不导出** `SkillDevService` / `SkillDevPipeline` / `SkillDevDeps` / `StateStore` / `WorkspaceProvider` → `server/runtime/agent_adapter/interface.py` `_get_skilldev_service` 的包级 import 失败。
3. 由 ①，`_SKILLDEV_METHODS`（`frozenset(m for m in ReqMethod if m.value.startswith("skilldev."))`）为**空集合** → server/runtime/agent_adapter/interface.py 的流式 / 非流式 skilldev 分支**永不命中**。

即使命中，异常也被 server/runtime/agent_adapter/interface.py 的 `except Exception` 捕获转为 `skilldev.error` chunk，不崩主链路。Agent 接入层（`context.create_stage_agent` / `_register_tools`）全部 `NotImplementedError`。模块自带完整设计文档 `DESIGN.md`（含完整待办表），是设计意图的权威参考。

## 已知风险清单（接线前必须处理）

| 风险 | 位置 | 说明 |
|---|---|---|
| VALIDATE↔GENERATE 无重试上限 | pipeline / validate_stage | 占位 GENERATE 产出必然校验失败 → 理论死循环（有 checkpoint 落盘不卡进程但永不收敛）；接线时补 retry_count 上限 |
| StateStore 无原子写 | store.py | `write_text` 直写，崩溃可产生半写 state.json；对照 [[S_02]] N1 的 temp+`os.replace`+fsync 模式修 |
| `_handle_file_read` 路径前缀校验可绕过 | service.py | `startswith(skill_dir)` 缺尾部路径分隔符——`.../skill_x/` 兄弟目录可通过；改用 `relative_to` 比较或补分隔符 |
| `_handle_cancel` 的 `await AgentResponseChunk(...)` | service.py | 对 dataclass 实例 await，走到即 TypeError |
| `_now_iso` 用已弃用的 `utcnow()` | schema.py | naive UTC；与 artifact_security 的 timezone-aware 口径不一 |

## 不变量

- **N1（确定性骨架）**：pipeline 是确定性状态机——所有 LLM 能力经 `SkillDevDeps` 注入的模型配置 + `context.create_stage_agent` 获取，骨架自身不 import 具体模型 / SkillManager（接线时也不得破坏）。
- **N2（挂起即 checkpoint）**：任何挂起暂停前必须落盘 state.json；恢复前必须校验当前阶段确为挂起点。
- **N3（stage handler 无跨请求状态）**：Handler 不得持有跨请求状态；每请求新建 pipeline 实例，执行到挂起 / 终态即释放。
- **N4（`skilldev.respond` 单一恢复入口）**：三个挂起点共用一个 respond 语义；新增挂起点只改 `SUSPENSION_POINTS`。

## 修改检查单

- 接线工作（未来）→ 先补三重断点（ReqMethod 成员 + `__init__` 导出 + 路由集合），再逐阶段替换占位实现；每阶段先补"能复现占位行为"的测试。
- 改状态机 / 挂起点 → 核对 `SUSPENSION_POINTS` 与 `compute_todos`（Todo 列表唯一计算来源）同步。
- 改事件 → 每个事件必须能映射到前端 UI 动作（设计原则）；新增事件类型 = 前端契约变更。
- 改 store / workspace 路径 → 核对与 DESIGN.md 的路径约定及 `interface._get_skilldev_service` 的构造参数一致。

## 与其它 spec 的关系

- `state_utils.py` 的账本语义归 [[S_02]]；产物 `.skill` 包最终经 [[S_03]] 的标准安装入口落地（当前手动）。
- 与 skill_turbo（加速执行已装技能）正交：skilldev 管"技能怎么被创造"，turbo 管"技能怎么被加速执行"。
