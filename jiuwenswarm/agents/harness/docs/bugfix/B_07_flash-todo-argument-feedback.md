# Flash todo 参数错误反馈

| 项 | 值 |
| --- | --- |
| 类型 | bugfix |
| 日期 | 2026-11-18 |
| 问题 | jiuwen_issue_6997 |
| 关联规约 | [S_05 profile todo 变体](../specs/S_05_profile-todo-variants.md) |

Flash 在更新待办进度时收到 `{"action":"modify","todos":[{"id":"research","status":"in_progress"}]}`。外层工具通过权限检查，子操作投影因缺少 `modify_action` 失败，导致模型收到通用权限拒绝并继续重复调用。

工具本体和投影共用操作参数校验。Flash 对已知 todo 参数错误返回结构化失败及字段提示，跳过该次执行。实际权限拒绝、有效参数的子操作检查以及会话和审批约束沿用既有行为。

回归覆盖模型消息及结果事件、归一开关、同批合法调用、外层 DENY、补参后的子操作审批和失败结果对收尾状态的影响。产品场景需另行验证实际模型收到提示后的行为。
