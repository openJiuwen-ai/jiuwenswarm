# A2UI Text 的 usageHint 与组件 id 同名导致渲染失败

| 项 | 值 |
|---|---|
| 日期 | 2026-11-27 |
| 范围 | A2UI v0.8，Web 和 OfficeClaw |
| 关联 spec | [S_03_runtime-agent-management.md](../specs/S_03_runtime-agent-management.md#a2ui-协议) |
| 来源 | [Issue #6880](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6880) · [MR !10659](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10659) |

A2UI Web SDK 0.8.0 将与组件 id 同名的属性字符串解析为组件引用。若父级 Column 的 id 为 `body`，子级 Text 的 `usageHint` 是 `body`，解析子级时会再次访问父级。服务端此前通过该输出的校验，前端在构建组件树时抛出循环依赖错误。Text 的 id 与自己的 `usageHint` 同名时也会触发该错误。

中英文生成提示词要求任何组件 id 避开 Text 的七个 `usageHint` 枚举值。终稿校验检查实际出现的值，按消息顺序跟踪每个 surface 的组件定义。出现同名时，复用已有修复流程，要求重命名冲突组件及其引用，并保留 Text 的 `usageHint`。同一回复内跨协议块的更新也参与校验，删除 surface 会清除其组件状态。

Web Core 0.8 的 `surfaceUpdate` 按 id 新增或覆盖组件，未出现的 id 会保留，因此校验沿用相同的更新语义。Text 省略 `usageHint` 时没有参与引用解析的字符串，渲染阶段的默认样式不会触发冲突。终稿同名检查覆盖显式的 `Text.usageHint`。选项值和按钮 action context key 继续由已有提示词约束。

定向回归覆盖七个枚举值、父级冲突、跨协议块更新、surface 隔离与删除。Web 和 OfficeClaw 的实际终稿入口使用模型替身验证，第一次修复仍有冲突时会拒绝，第二次返回有效组件树后才会通过。A2UI 单测、Flash 提示词离线测试及 A2UI 系统用例共 176 通过、1 跳过。
