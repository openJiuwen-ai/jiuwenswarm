# Flash Todo 输出兼容

日期：2026-10-27。关联需求：FE20261014380870。适用范围：Flash及其共享事件/历史出口。

Todo文件的内部版本不能成为旧接入方必须理解的新字段。`todo_snapshot`按宿主身份与服务端
`todo.snapshot_modes`配置选择输出，缺省legacy；模型或请求参数不能启用版本输出。
实时事件与历史共用`project_todo_snapshot`，均来自同一次提交或同次权威读取。

- legacy保留generation过滤、串行状态遮罩和原字段；实时输出按工具意图选择执行或提醒查询视图，历史读取合法空表时发送空数组。
- 显式versioned保留真实状态和匹配的`todo_meta`，包括合法空表；同次提交的 task.update 与 todo.updated 使用一致的展示清单，取消项按既有 Todo 展示规则过滤。
- 读取失败返回不可用状态，不能冒充清空；提交快照不进入模型工具返回JSON。
- 共享 task.update 出口同样按接入模式处理 generation 和串行遮罩，提醒仅在显式展示时计入；task.start/complete 在 legacy 下不携带内部元信息。内部计划校验仍在 SDK 事务中执行，不因旧协议输出而取消。

契约见[会话快照规约](../specs/S_14_session-lifecycle-and-upgrade.md)和
[配置规约](../specs/S_08_config-and-environment.md)。本地测试覆盖两种投影、并行连接隔离、
连接内配置冻结和实际HTTP分发；不能据此宣称现有云上ChatService已通过实测。
