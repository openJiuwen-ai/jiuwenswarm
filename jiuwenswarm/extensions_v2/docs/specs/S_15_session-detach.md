# session.detach 界面会话关联解除规约

| 项 | 值 |
| --- | --- |
| 编号 | S_15 / session-detach |
| 最近修订日期 | 2026-10-29 |
| 特性与验收 | [F_26](../features/F_26_session-detach.md) |

## 1. 事件输入输出

输入 SessionDetachInput 为 frozen dataclass。全部字段由平台从已解除的旧关联填入，用户不填写、不改写，原样 next；下表字段均为必填构造参数、无默认值。

| 字段 | 类型 | 含义、来源与取值 |
| --- | --- | --- |
| session_id | str | 已解除关联的旧主会话 ID，来自平台关联表，不是切换后的新会话 |
| surface_id | str | PR08 建立关联时的 view_id，表示具体视图；不是 UI 卡片/渲染槽位 ID |
| attachment_id | str | PR08 为旧关联生成的 32 位十六进制 UUID；用户用它定位旧资源，不用 surface_id 单独删除新资源 |
| channel_id | str | 旧关联所属渠道，例如 web；不是 WebSocket 连接或设备标识 |
| mode | str | 建立旧关联时平台确认的模式，例如 agent.work.normal，不代表退出后的新模式 |
| reason | str | switch 表示一次已确认的新目标提交替换旧关联（包括离开已跟踪范围）；detach 表示匹配身份的显式业务请求已移除关联。无 end 值 |
| next_session_id | str 或 None | switch 到另一个已跟踪主会话时是新会话 ID；主动解除或离开未跟踪范围时为 None |

输出 SessionDetachOutput 为 frozen dataclass，全部由默认 CORE 填写。所有字段必填、无默认值，用户原样返回真实回执。

| 字段 | 类型 | 含义与消费方式 |
| --- | --- | --- |
| accepted | bool | 严格 True，仅确认旧 attachment 已不再是当前关联；不代表所有用户资源均清理成功 |
| session_id | str | 回显旧会话 ID，用于核对，不能指定另一个目标 |
| surface_id | str | 回显被解除的具体视图 |
| attachment_id | str | 回显被解除的旧关联身份；新关联不复用它 |

## 2. Server 业务请求

请求方法 session.detach，由现有跨进程请求路由交给 Worker。接入者从 attach 通知取得关联身份；channel_id 沿用现有已路由请求，不接受 params 中覆盖。它不会由用户直接 trigger 一个无平台调用作用域的事件来替代。

| params 字段 | 类型 / 默认 | 应填写什么 |
| --- | --- | --- |
| session_id | str，必填、无默认 | 要离开的旧主会话 ID；必须与平台当前该视图的关联一致，不回退使用其他会话 |
| view_id | str，必填、无默认 | attach 输入中的 surface_id，1～128 个 ASCII 字母/数字或 ._:- |
| attachment_id | str，必填、无默认 | attach 输入中的原始 32 位小写十六进制 UUID；必填以防旧请求移除重连/重新关联后的新资源 |

成功响应 payload 包含 session_id、surface_id、attachment_id（回显请求身份）和 detached: bool。True 表示本请求实际移除了匹配关联；False 表示当前没有关联，无重复通知。另一个关联已占用该视图时返回 STALE_ATTACHMENT，保留新关联；字段缺失/格式不合法返回 BAD_REQUEST；Worker 关联服务未就绪返回 NOT_READY。成功响应不代表用户的全部外部清理成功。

## 3. 不变量与边界

- SD-01：先提交解除或替换，再通知；只触发一次旧关联的 detach，不使用请求自报 previous_session_id。默认 CORE 只确认事实，不执行第二次移除。
- SD-02：按 channel_id + view_id 区分，匹配旧 attachment_id 后只移除这一项。同会话其他视图、平台历史、Agent 和会话目录保持原业务管理。
- SD-03：切换先发布旧关联 detach，再发布仍然有效的新 attach；重复同目标提交、稳定 view_id 重连不产生 detach。通知期间出现更晚替换，旧清理仍针对自己的 attachment_id，失效新 attach 不获得成功确认。
- SD-04：业务等待预算 2 秒，从通知任务启动计；用户异常/超时不恢复关联，不阻止原业务继续。原调用取消传播，已提交事实保持；忽略取消的任务不能取得迟到有效 CORE 回执，在途计数保留至退出。已发生的外部副作用不回滚。
- SD-05：输入所有字段只读，提前伪造/后置修改成功无效；原样 next 继续本条链。无新增 Ops；平台提供旧关联绑定的只读 SessionContext，不依赖用量事件，也不因会话已不可读而伪造目录/模型。
- SD-06：不将传输断开、页面隐藏、KVC 回收/暂停推断为解除。不新增前端自动关闭处理，不提供 surfaces Ops，不与 PR10 的 session.end 联动；无 Team/子 Agent 新扩展。内存事实不承诺跨重启恢复或持久 exactly-once。
