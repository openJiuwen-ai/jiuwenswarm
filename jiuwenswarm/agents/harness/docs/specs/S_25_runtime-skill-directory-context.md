# 运行时提示中的技能目录

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_25 / runtime-skill-directory-context |
| 关联模块 | `common/rails/runtime_prompt_rail.py`、`common/prompt/prompt_builder.py`、`jiuwenswarm/server/runtime/agent_adapter/interface_deep.py` |
| 最近一次修订日期 | 2026-11-22 |
| 关联 feature | 暂无 |
| 关联 bugfix | `../bugfix/B_08_runtime-skill-directory-context.md` |

## 范围 / 边界

本规约覆盖模型提示中的内部技能存储路径、当前会话加载路径，以及 OfficeAce 安装工具的职责说明。技能安装、注册、启停和工具错误处理由各自的现有实现负责。

## 不变量

- N1 内部存储路径使用 `get_agent_skills_dir()` 或显式 Agent 工作区的 `skills` 子目录，显示为“JiuwenSwarm 内部技能存储目录”。
- N2 DeepAdapter 提供的加载路径取自当前 `SkillUseRail.skills_dir`，包含该组件接收的共享、预置及扩展技能根目录。
- N3 每次 `before_model_call` 读取目录提供回调，技能组件替换后使用新组件的目录。当前组件为空时显示“暂无可用目录”，不推断组件缺失的原因。
- N4 加载目录列表保持原顺序。目录列表用于说明技能读取位置，安装目标由宿主安装工具选择并注册。
- N5 中英文静态身份说明与运行时目录说明使用一致的路径语义；加载目录指引以运行时明确提供目录为条件，不假定所有调用方都提供回调。

## 接口契约

`RuntimePromptRail` 构造函数增加可选参数 `skill_dirs_provider`，类型为 `Callable[[], str | list[str]] | None`。

回调返回当前加载组件的一个目录或目录列表。空列表表示当前没有可用的加载目录，不区分未启用、装配失败等原因。省略回调的调用方继续展示内部存储信息，加载目录由显式提供回调的调用方说明。

## 数据结构

`RuntimePromptRail._skill_dirs_provider` 保存读取入口。DeepAdapter 的回调在调用时读取 `self._skill_rail`，组件的生命周期由现有适配层管理。运行时提示按模型调用刷新，路径信息保存在现有目录提示 section 中。

## 执行路径全景

1. `interface_deep.py::_build_skill_rail` 使用 `_resolve_skill_dirs` 创建技能加载组件。
2. `interface_deep.py::_build_runtime_prompt_rail` 将读取当前组件 `skills_dir` 的回调传给运行时提示。
3. `runtime_prompt_rail.py::_format_agent_data_paths` 生成内部数据目录说明。
4. `RuntimePromptRail.before_model_call` 读取回调，追加“当前会话技能加载目录”。`officeclaw` 渠道同时说明宿主安装工具选择目标目录并注册。
5. 提示通过原有 `tui_current_project_policy` section 进入模型上下文。

## 异常分支与退出路径

| 场景 | 行为 |
|---|---|
| 当前技能组件为空或回调返回空列表 | 加载目录显示“暂无可用目录”，不推断原因 |
| 调用方省略回调 | 展示内部数据路径 |
| 技能组件替换 | 下一次模型调用读取替换后的组件 |
| 宿主安装工具报错 | 由既有工具调用流程返回错误 |

## 边界 case 清单

| 场景 | 预期行为 | 测试入口 |
|---|---|---|
| 独立工作区 | 加载目录与工作区技能组件一致 | `test_runtime_prompt_skill_dirs_match_active_loader` |
| OfficeAce 共享目录 | 加载列表包含产品及用户目录 | 同上 |
| 企业预置技能及扩展根目录 | 使用技能组件收到的目录和顺序 | 同上 |
| 组件替换、单字符串目录、组件移除 | 当前提示及时更新 | `test_runtime_prompt_tracks_replaced_and_disabled_skill_loader` |
| 中英文与自定义内部存储路径 | 路径语义一致，内部路径遵循现有路径解析 | 上述用例及 `test_runtime_rail_path_provider_memory_override` |

## 修改检查单

- 修改目录标签时同步核对 `prompt_builder.py` 的中英文身份说明。
- 修改技能组件重建或扩展目录注入时，核对运行时提示仍然读取当前组件。
- 修改回调消费时，运行提示结构、无项目工作区与事件循环响应的已有测试。

## 与其它 spec 的关系

Agent 实例及租户隔离遵循 `jiuwenswarm/server/runtime/docs/specs/S_01_agent-instance-lifecycle.md`。共享技能配置的热更新遵循同目录 `S_03_config-hot-reload.md`。本规约说明这些状态进入模型提示时的语义。
