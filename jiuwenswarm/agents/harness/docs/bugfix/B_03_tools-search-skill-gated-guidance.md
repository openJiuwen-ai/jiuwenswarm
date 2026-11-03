# B_03 — skill 未激活时 tools_search 无匹配裸文案缺失重载指引，续跑降级到模型研究路径

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_03 / tools-search-skill-gated-guidance |
| 关联模块 | `jiuwenswarm/agents/harness/common/rails/progressive_tool_rail.py` |
| 修复日期 | 2026-11-04 |
| 关联 spec | 无（全量 grep `docs/specs/` 实证：progressive tool rail 的 deferred 可见性与 required_skill 门控不在 S_01~S_21 已归档域内，无需 spec 修订） |
| 关联测试 | `tests/unit_tests/agentserver/test_skill_required_tool_visibility.py` |
| 配套修复 | HANDOVER `deepsearch skill_tool 口径统一` 线 fix-e；fix-a/b/c/d 为前置系列（invoke_tool 门控拒绝文案 / SKILL.md 指引等，见仓库根 `HANDOVER_deepsearch_skill_tool口径统一.md`） |

## 复现场景

officeClaw 部署环境 DeepResearch 中断续跑（full.log，进程 5556，2026-11-04）：

1. 09:52:08 会话中断，stopped 转译生效；同刻 skill 激活态重置（可见工具 121→14）。
2. 09:52:09 用户「继续」；模型为找回 deepresearch_execute 调
   `tools_search(tool_name="deepresearch_execute")`。
3. skill 未激活 → deferred 搜索缓存被过滤 → 无匹配，返回裸文案
   「未找到名为 'deepresearch_execute' 的按需可见工具，请检查名称是否与导航列表一致。」
   ——无任何 skill_tool 重载指引。
4. 模型挣扎 45 秒（上下文压着引擎历史失败记忆 212000/211901 与上一轮助手
   「方案 2 手动研究」），09:53:35 todo create 分解手工研究任务，降级到模型
   研究路径——已部署的 DeepResearch 能力被绕过。

触发条件：会话中断 / 闲置过期 / 会话切换导致 skill 未激活后，模型在续跑轮次
通过 tools_search 找回门控工具。

## 根因

skill_tool 口径统一线的收尾缺口——三条信号通道只堵了两条：

1. **tools_search 无匹配文案无重载指引**（本修复）：skill 未激活时
   `_refresh_deferred_tool_cache` 的过滤条件把 declared（声明 required_skill）
   工具排除在 deferred 搜索列表外，tools_search 必然无匹配；最终 return 的
   else 分支只提示「检查名称是否与导航列表一致」，模型无从得知该走
   skill_tool 重载。
2. **invoke_tool 拒绝文案已修（fix-b）但不可达**：拒绝分支已带重载指引，但
   工具不在 function definitions 里，模型在续跑场景不会发起 invoke_tool。
3. **SKILL.md 指引是死路径**：触发条件写「若 deepresearch_execute **返回**
   工具不可用」，而工具不可见时该返回永不出现。

三通道中 tools_search 是续跑场景模型唯一会真实触发的信号入口；该入口缺失
重载指引时，模型在记忆压秤（引擎历史失败）下选择降级。

## 修复口径

`progressive_tool_rail.py` `_search_tools` 最终 return 前（dead-instance 分支
之后）插入反查逻辑，复用 invoke_tool declared 分支（fix-b）已验证的模式：

1. 无匹配时反查全量注册表：`_cached_all_tool_infos`（unfiltered，skill 未
   激活时门控工具卡片仍在其中）。`_cached_deferred_tool_infos` 恒为它的
   过滤子集（同函数内成对构建），无需二次兜底查找。
2. `_required_skill_requirement` 提取 required_skill，命中有效值即返回对齐
   fix-b 口径的重载指引：点明工具属于哪个技能、泛化重置成因（技能可能因
   闲置过期或会话切换而未激活）、给出明确调用方式
   `skill_tool(skill_name="<skill>")`、明示「这属于技能未激活，不代表能力
   不可用，不要据此降级或改用其他工具绕过」。
3. 普通工具（反查无 required_skill、或 declared 但值为 malformed）保持原
   通用文案不变——零行为变化，fail-safe 到现状（`_required_skill_requirement`
   对 None 输入安全返回 `(False, None)`）。

检视修复（2026-11-05）：required_skill 命中后须先比对激活态再下结论——
`_refresh_deferred_tool_cache` 对 declared 卡片无条件排除（不区分激活态），技能
已激活时延迟缓存同样 0 命中，反查全量注册表命中 required_skill 后若直接断言
「技能未激活」与事实相反，会诱导模型对已激活技能重复执行 skill_tool 重载（若
重载有副作用还会打断进行中的研究上下文）。修复：命中 required_skill 后比对
`_current_active_skill()`，已激活时返回「工具已随技能激活并直接可见，请直接调用，
无需 tools_search」；未激活才保留原重载指引。disabled 前置检查在前（「已禁用」
文案不进反查分支），不受影响。

## 验证

- **新增测试**（`test_skill_required_tool_visibility.py`，门控行为专属文件）：
  - `test_inactive_skill_search_returns_skill_reload_guidance`：用
    deepresearch_execute 真名复现线上故障——skill 未激活时 tools_search 断言
    返回含 `skill_name="deepresearch"` 与「不要据此降级」的重载指引，而非
    裸「未找到」。
  - `test_ordinary_missing_tool_search_keeps_generic_message`：非门控工具
    无匹配保持通用文案，不误报技能重载指引。
  - `test_active_skill_search_reports_direct_visibility_without_reload`
    （检视修复新增，2026-11-05）：`active_skill_provider` 返回激活技能名时，
    tools_search 命中该技能门控工具返回「已随技能激活并直接可见」的直接调用
    提示，文案不含 skill_tool 重载指令——激活态不再误报「技能未激活」。
- **fail-safe 既有覆盖**：`test_invalid_required_skill_fails_closed_everywhere`
  参数化组（None / "" / 42 / 路径穿越等）——malformed required_skill 反查得
  None 落回通用文案。
- **回归**：`test_skill_required_tool_visibility.py` 30 passed；progressive
  rail 相关六文件（code_adapter / disabled_filter / deepresearch_context /
  prefix_cache / stale_id / inherit_flag）156 passed；
  `test_request_scoped_office_claw_mcp.py` + `test_deepresearch_execute_tool.py`
  225 passed（含「tools_search 0 matches 后 LLM 回退 web_search」既有场景，
  确认不误伤）。合计 411 passed / 0 failed（.venv）。
- **检视修复回归**（2026-11-05）：`test_skill_required_tool_visibility.py` +
  `test_deepresearch_execute_tool.py` → **172 passed**（.venv，含激活态新用例；
  未激活用例断言保持不变）。

## 防错教训

- 面向模型的错误文案是行为契约的一部分：同一事实（技能未激活）在多条信号
  通道（tools_search / invoke_tool / SKILL.md）上的口径必须统一——任何一条
  通道缺失重载指引，都可能让模型在记忆偏差压秤下选择降级路径。
- 文档指引的触发条件必须对应真实可达的信号：SKILL.md 写「若工具返回不可用」
  但工具不可见时该返回永不出现，等于把指引写在死路径上。
- 「搜索无匹配」不等于「能力不存在」：反查 unfiltered 全量注册表可区分
  「技能未激活」与「真未注册」两种无匹配，前者的正确响应是重载而非降级。

## 遗留与后续

- officeClaw 线上部署需同步 harness 侧 `progressive_tool_rail.py` 后自然恢复；
  纯文案补齐，不依赖线上数据迁移。
- fix-e 落地后 skill_tool 口径统一线（fix-a/b/c/d/e）在 jiuwenswarm 侧收口；
  线上验证需在中断续跑场景观察模型收到重载指引后是否走 skill_tool 而非降级。
