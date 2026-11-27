# B_07 — A2UI 组件被两个父级挂载时校验放行，前端渲染出两个联动的相同控件

## 元信息
| 项 | 值 |
|---|---|
| 日期 | 2026-11-27 |
| 严重程度 | 中：A2UI 卡片出现重复且联动的控件，提交结果仍正确；仅影响违反单亲约束的模型输出 |
| 范围 | `jiuwenswarm/server/runtime/a2ui/validator.py`；`tests/unit_tests/a2ui/test_validator.py`、`test_response_finalization.py` |
| 测试基线 | `pytest tests/unit_tests/a2ui/ -o addopts="--asyncio-mode=auto" -q` → **145 passed, 1 skipped**（2026-11-27） |
| 关联 spec | 无（不改变对外契约；prompt 中已有单亲约束，本次只让校验执行它） |
| 关联问题单 | BUG20261124415272 |

## 现象
BUG20261124415272（1.7.0）：A2UI 方案选择卡片中出现两个完全相同的下拉框，修改任一个，另一个同步变化。

## 根因
模型输出中 `opt`（MultipleChoice，`selections.path=/form/fix`）同时出现在 `g1` 与 `main_col` 两个 Column 的
`children.explicitList`。A2UI v0.8 每个引用渲染一个实例，两个实例绑定同一路径，于是显示与修改同步。
prompt（`prompt_instructions.py`）已要求单亲树，但 `validate_a2ui_messages` 只执行 SDK schema/拓扑校验；
SDK `analyze_topology` 只拦环与孤儿，不拦多父挂载。因此 finalization fast-path 判为 `schema-valid` 直接放行，
repair 未触发。日志签名：`A2UI response finalization fast-path schema-valid`。

## 决策
在 SDK 校验之后新增 `_validate_single_parent_tree`：同一 `surfaceUpdate` 内，每个组件 id 至多被直接挂载一次
（`child` / `children.explicitList` / `tabItems[].child` / Modal children）；`template.componentId` 不计入。
复用 SDK `get_component_references` 解析引用，报错点名组件与全部挂载点，违规内容走既有 repair → 文本降级链路。

## 拒绝的方案
- 只加强 prompt：已有约束仍被违反，必须有校验兜底。
- finalization 确定性去重：静默改写模型输出，保留哪个父节点无法通用判定。
- 前端按 id 去重：三方渲染库、跨仓、治标不治本。
- 修改 SDK `analyze_topology`：三方包且版本锁定。
- template 共享也判违规：不造成联动，会误伤合法卡片。

## 验证
- 新增用例覆盖本案结构、对照、同父重复、Button/Tabs/Modal 与 Column 双挂、template 共享保护、finalization 进入 repair。
- 必跑结果：`145 passed, 1 skipped`（`tests/unit_tests/a2ui/`）。

## 已知遗留
历史已落库卡片重放仍显示双框；命中时增加一轮 repair 延迟；不覆盖跨消息增量更新造成的多父挂载。
