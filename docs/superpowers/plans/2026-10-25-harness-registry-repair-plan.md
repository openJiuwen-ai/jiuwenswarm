# Harness 注册与 Repair 一致性修复方案

> **For agentic workers:** Use `executing-plans` to implement the approved tasks sequentially. This document is a proposal; it does not authorize implementation, test execution, commits, or service restarts.

**Goal:** 消除 Prompt section 重复绑定、Prompt/Skill 路径错误，以及校验失败被记为内容未变化的问题。

**Architecture:** 在 RSI member optimizer 内共享注册规范和安全路径解析。Action、Repair 和 verifier 使用同一规则；最终结果仍交给原生 Plugin 加载和绑定约束验证。既有正常包保持兼容，修改限制在候选工作区及声明的可写文件内。

**Tech Stack:** Python、PyYAML、原生 Plugin loader/binder。

**Spec:** 本聊天中针对任务 `rsi-034b7026` 的问题 1、2及用户“先给修复方案”的要求。

## 范围与约束

- 当前只提供方案，不修改实现、不改历史实验产物。
- 使用 agent-core `/Users/jiuwen/orca/workspaces/agent-core/release-v0.1.20`。
- 保留已有工作区改动、评分和晋级规则、修复次数与无进展上限。
- 不修改已发布包、冻结基线、历史评分或全局模型配置。
- 不靠静默覆盖冲突项或删除功能来通过校验。
- 原生 loader 继续作为加载语义依据，不另建宽松加载器。
- 本轮不新增或运行测试；以下验收场景供后续获授权的验证使用。

## 现有根因

1. `_append_manifest_entry` 按原始文件路径去重；同一文件的不同写法会留下同名 section。自动注册还可能给同一项附加不同 priority。
2. verifier 检查文件存在和 Plugin 对象解析，没有完整检查有效 section 名称集合的绑定冲突。
3. Action、verifier、Repair 对路径基准和 schema 的理解不同。常见错误包括 `files/files`、Prompt `path` 字段和 Skill 挂载到 `SKILL.md` 文件。
4. Skill 自动挂载逻辑保留错误旧项，再追加正确项，整包依然无法加载。
5. `continuous.py` 在候选生成失败、回退父包后按包指纹记录 `candidate_content_unchanged`，掩盖原始失败并可能复用错误决策。

## Task 1：共享注册规范与冲突规则

**Files**

- Create: `openjiuwen/rsi/harness_rsi/member_optimizer/package_registry.py`
- Modify: `openjiuwen/rsi/harness_rsi/member_optimizer/action_executor.py`

**Interfaces**

- `normalize_package_registries(root: Path, *, writable_paths: list[str]) -> RegistryNormalizationReport`
- `validate_package_registries(root: Path) -> list[RegistryIssue]`
- `upsert_prompt_section(root: Path, *, name: str, file_ref: str, priority: int | None, writable_paths: list[str]) -> None`
- 报告包含被修改的文件、原引用、规范引用、错误码和冲突位置；错误不带敏感配置内容。

**规则**

- 新写入的 Prompt `file` 使用包根目录相对路径，如 `prompt_sections/files/a.md`。
- 新写入的 Skill `dir` 指向实际目录，如 `skills/foo`；保留有效挂载选项及无关条目。
- 兼容可唯一定位现有文件的历史简写；重复前缀只在已知资源目录内、候选目标唯一且存在时修正，禁止按 basename 全包猜测。
- 绝对路径、`..`、符号链接逃逸和多个候选目标均拒绝。
- Prompt 的 `path` 只在无歧义时转换为 `file`；并存字段若指向不同目标则报冲突。
- 同名、同文件、同配置的重复项合并；重复执行结果不变。
- 同名不同文件，或同名且 priority/render 参数冲突，报明确冲突，不采用“最后一个覆盖”。
- Action 对其自己的注册项按显式计划约束更新；未指定的可选配置沿用已有唯一合法值。
- 新增操作遇到已存在且指向其他资源的名称，应拒绝；修改操作按显式目标更新，保留无关条目。
- Skill 条目错误指向 `SKILL.md` 时，仅在文件确实存在且目录可被原生发现时转为其父目录。
- 统一处理错误旧项，不能只追加正确项而保留坏引用。

- [ ] 实现共享规则及结构化错误。
- [ ] 将 Action 自动注册接入共享 upsert/规范化，保持声明写入边界。
- [ ] 对照真实失败清单人工审阅规范化前后结果及幂等性。

## Task 2：让工程校验覆盖实际加载与绑定约束

**Files**

- Modify: `openjiuwen/rsi/harness_rsi/member_optimizer/verification.py`
- Modify: `openjiuwen/rsi/harness_rsi/member_optimizer/action_executor.py`

**Consumes:** Task 1 的注册规范及错误报告。

**Produces:** Action 合并前的注册错误反馈、整包的可修复绑定错误。

- [ ] Action 写入后立即检查其涉及的 registry、资源存在性、schema 和名称冲突，失败反馈给同一 Action Agent，在已有预算内修正。
- [ ] 子工作区合并后再次校验，捕获分别合法但合并后发生的冲突。
- [ ] 整包校验复用原生 Plugin loader，检查主 manifest、sidecar 等来源合并后的有效 section 名称集合。
- [ ] 复用运行时绑定约束，覆盖与宿主已有 section 的冲突；不执行 case、不调用 LLM、不运行候选 hook。
- [ ] 将 `prompt_section_duplicate`、`resource_path_invalid`、`registry_schema_invalid` 等定位到具体文件和条目，允许 Repair 接收。

## Task 3：Repair 使用相同规则和结构化反馈

**Files**

- Modify: `openjiuwen/rsi/harness_rsi/member_optimizer/verification.py`
- Modify: `openjiuwen/rsi/harness_rsi/member_optimizer/agents/prompts/verification_repair.md`
- Modify: `openjiuwen/rsi/harness_rsi/member_optimizer/agents/prompts/action_execution.md`

**Consumes:** Task 1、2 的规范、错误码、实际存在的资源及冲突项。

**Produces:** 修复后重新通过相同校验的候选，或带完整原因的修复失败记录。

- [ ] Repair 输入明确列出包根目录、正确 schema、原引用、解析目标、可修正目标和当前失败检查。
- [ ] 在候选工作区内先处理唯一、确定的机械性修正，再交给 Agent 处理其余冲突；规则不替 Agent 选择不明确的内容或 priority。
- [ ] Repair 编辑后调用与 Action 相同的规范化及校验，再做整包加载/绑定检查。
- [ ] 成功以校验结果为准；Agent 声称 repaired 不能绕过失败。
- [ ] 保留原有修复次数和无进展停止规则；预算耗尽记录剩余错误，不空转。
- [ ] 同步 Action/Repair 提示词，使用相同的完整合法示例，明确 `file` 与 `dir` 的区别。

## Task 4：保留候选失败根因

**Files**

- Modify: `openjiuwen/rsi/harness_rsi/single_harness/continuous.py`

**Consumes:** member optimization 状态、role_results 和 verification/fix_result 产物。

**Produces:** 可追溯的候选失败原因，不混淆为内容未变化。

- [ ] 在包指纹比较、重复试验决策复用之前检查候选生成状态。
- [ ] 校验、执行或规划失败分别记录原始阶段、错误和证据路径；沿用现有规划拒绝语义。
- [ ] 生成失败不登记为有效包的同内容评估决策。
- [ ] 只有生成成功且内容确实与父包相同，才使用 `candidate_content_unchanged`。
- [ ] 保持父包和 Best 不变，按原有候选尝试预算继续；不改变分数门槛和晋级判据。
- [ ] 同步中英文流程说明。

## Review Focus 与验收场景

1. **路径别名和幂等性**：`a.md`、`files/a.md`、`prompt_sections/files/a.md` 唯一指向同一文件时，输出一个规范注册；第二次执行无额外差异。
2. **真实冲突不丢失**：同名不同文件/不同 priority 不静默覆盖；有明确 Action 更新约束时只更新该项，无关项保持。
3. **路径与 schema 错误**：覆盖重复前缀、`path` 别名、Skill 文件挂载、缺文件、歧义、绝对路径、`..` 和符号链接逃逸。
4. **运行时一致性**：覆盖 sidecar 与主 manifest 的重复名称、合并后冲突、宿主名称冲突；有效包仍能正常加载。
5. **状态可追溯性**：生成失败应保留验证错误，不变成内容未变化或其他 case 的缓存拒绝；修复耗尽保持父包并正常推进。

获授权执行验证时，使用临时副本重放任务 `rsi-034b7026` 的 m001/m004/m007 错误包，并将 m002 发布包作为兼容性样本。只做本地加载和绑定验证，不重新评分、不修改历史任务、不发模型请求。

## 自查结论

- 两个请求的问题均覆盖生成、合并、Repair、工程校验和结果记录。
- 没有改动通用 loader 的历史路径优先级；规范化发生在 RSI 可写候选工作区。
- 没有将不明确的冲突转化为静默修复；没有放宽运行时绑定规则。
- 方案经用户批准后已实施任务 1–4；进行了模块导入检查和独立只读代码审阅。
- 审阅发现的路径歧义、JSON Skill 父挂载问题已纳入同轮修正。
- 功能验收场景尚未执行；未运行测试、提交或重启服务。
