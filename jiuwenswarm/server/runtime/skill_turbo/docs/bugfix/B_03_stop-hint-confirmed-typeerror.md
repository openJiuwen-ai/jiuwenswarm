# _SKILL_TURBO_STOP_HINT_CONFIRMED 类型错误：字符串按 dict 索引致已确认收尾必崩

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_03 / stop-hint-confirmed-typeerror |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/skill_turbo_tools.py`（`_SKILL_TURBO_STOP_HINT_CONFIRMED` 定义 / `_wrap_skill_turbo_result` 使用点） |
| 归档日期 | 2026-10-22 |
| 关联 spec | S_07_fallback-and-degradation.md（收尾三分支 + 修改检查单"改收尾三分支"） |
| 发现渠道 | 线上批量测试（2026-10-22 xlsx/docx turbo 交付后 tool 返回 success=false + TypeError traceback） |

## 复现场景

非 PPT 技能（docx D8 / xlsx X9 等）交付节点产物 info 声明
`send_file_status == "sent"` 且本次 skill_acceleration_exec 成功收尾时，
`_wrap_skill_turbo_result` 走通用交付确认分支：

```python
parts.append(_SKILL_TURBO_STOP_HINT_CONFIRMED[lang])
```

常量定义为**纯字符串**（括号隐式拼接），按 `[lang]`（"cn"/"en"）索引必然抛
`TypeError: string indices must be integers, not 'str'`——工具返回
success=false + traceback，外层 LLM 拿到失败信号后行为不可控（实测：外层
agent 手动重做全任务浪费约 6 分钟；另一次降级续跑产出不受控）。

2026-10-22 全天 10 次成功交付的 turbo 运行（docx ×2、xlsx ×8）**全部**
触发；即该分支自引入（case_3 docx D8 重复发送修复）起从未成功执行过。

既有单测假绿逃逸：`test_success_with_confirmed_delivery_uses_confirmed_hint`
的 holder 用了 `{"deliveries": [...]}`（list）形状，与 executor
`_collect_node_artifact` 真实产出 `{plan_name: {"info": {...}}}` 不符，
`_generic_delivery_confirmed` 判定不过 → 实际走 NEUTRAL 分支，从未触达
本分支；断言 `"未确认" not in result` 又恰好被 NEUTRAL 文案的
"未**能**确认"骗过（非连续子串）。

复现用例：修正 holder 形状后的同名单测 + 新增语言切换单测，修复前均红
（TypeError 与线上 traceback 逐字一致）。

## 根因

CONFIRMED 停止提示常量按 NEUTRAL 的使用方式（`[lang]` 索引 dict）编写，
定义却写成了括号隐式拼接的纯字符串（遗漏 `{"cn": ..., "en": ...}` 外壳）；
兄弟常量 `_SKILL_TURBO_STOP_HINT_NEUTRAL` 与 PPT 两提示常量均为 dict。
分支被唯一单测以错误 holder 形状绕过，typo 无法被测试网捕获。

## 修复口径

1. `_SKILL_TURBO_STOP_HINT_CONFIRMED` 改为 `{"cn", "en"}` dict：en 保留
   原文案；cn 新增（对齐 NEUTRAL cn 风格，语义 = 已确认发送 + 基于产物
   摘要总结 + 禁止重复调用/发送）。语言源复用 request metadata
   （`_resolve_stop_hint_language` 与 NEUTRAL 同源，两份停止提示保持
   同语言）。
2. 修正假绿单测：holder 形状对齐 executor 真实产出；断言改为区分
   CONFIRMED / NEUTRAL 的实质文案（"已确认文件"存在；"未能确认" /
   "不要声称文件已发送"不存在）。
3. 新增 en 语言切换单测（防 dict 缺 key 的 KeyError 回归）。
4. PPT 路径零影响：pptx-craft 交付骨架先行分支（`_ppt_delivery_summary`
   非空）在本分支之前拦截，本常量对 PPT 技能是死代码路径（S_07 边界 20
   维持不变）。

## 测试

`tests/unit_tests/server/runtime/skill_turbo/test_entry_gates_and_honesty.py`：
修正 `test_success_with_confirmed_delivery_uses_confirmed_hint`（真实
holder 形状 + 实质断言）、新增 `test_confirmed_hint_follows_request_language`
（en 切换）。修复前 2 红（TypeError 复现线上故障）；修复后模块单测 +
agentserver HITL/工具面共 449 用例全绿。
