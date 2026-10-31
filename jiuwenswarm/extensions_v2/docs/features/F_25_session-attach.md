# session.attach 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_25 / session-attach |
| 建档日期 | 2026-10-29 |
| 归档日期 | 2026-10-29 |
| 最近修订日期 | 2026-10-29 |
| 计划与进度 | [EXTV2-20 / Server PR08](../designs/extensions-v2-development-plan.md#extv2-20-界面与主会话关联建立通知) |
| 主责规约 | [S_14](../specs/S_14_session-attach.md) |

## 1. 范围、状态与依据

基于最新扩展目标 rsi_native_agent_os 的 7795851916dd5ce1d34b70f5a20d6d30eafc25e3，Core 使用 ead700d7d18a247968d64de54918cf5d201a207a。采用现有 Web view_id → Server 提交上下文 → Runtime foreground_changed 真实链路，不依赖尚未合入 PR04/05/06/10 或 Core !110。

源码已经有 session.create/session.switch 的提交事实，缺少独立的界面与主会话关联表和扩展通知。本特性补充 Worker 平台关联表、session.attach 事件/默认 CORE、宿主 foreground 接线和用户示例。实现与定向、真实服务验证已完成，结果见第 6 节。

## 2. 能力说明与 Claude 对照

session.attach 表示一个具体视图已经进入一个有效主会话。用户可据此初始化这个视图的会话展示、记录关联日志或读取既有只读 Ops；平台先提交关联事实，Worker EventBus 再调用用户 Handler。用户原样 next，CORE 校验当前关联并确认，不再次执行会话切换或启动聊天。

Claude 固定提交 bf2323b3021c9d3db7a31e1e7d61068c3f704497、声明头 2.1.277 的 session.attach 描述客户端加入会话，包含渲染面、客户端 ID 和可选 viewport，默认回显客户端身份。本地以已有 view_id 表示具体视图，另含会话/关联身份，不把 A2UI surfaceId、Gateway 连接或 ui.ready 冒充客户端关联；暂不提供 viewport 或终端隐式绑定，不承诺类型可直接互换。

## 3. 事件契约

完整字段、来源和填写责任见 [S_14 第 1 节](../specs/S_14_session-attach.md#1-事件输入输出)。输入与输出均为平台 frozen dataclass。用户只注册 `session.attach` 的 USER Handler，可按 channel_id/mode 等字段等值筛选；全部字段只读，原样 `return await next_handler(event)`。

create 在原响应交付后通过原后台任务提交关联，switch 在提交时走原 await 链。用户通知失败/2 秒超时不否决已经提交的关联；取消、迟到、并发更新和重放语义见 S_14。accepted 只确认默认 CORE 当时看到有效关联，不说明每个用户的展示均成功。

### 3.1 用户需要准备的完整包

以下两个文件放在 ZIP 根目录，经既有扩展管理入口安装。用户不注册平台事件类型/CORE，不构造 SessionContext。示例只日志；用户需要前端展示时另接自己的渲染消费链。

```yaml
schemaVersion: 1
package:
  id: example.session-attach
  version: 1.0.0
  displayName: Session Attach Example
extensions:
  - id: attach-observer
    runtime: backend
    module: extension.py
```

```python
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Observe committed views without starting chat or modifying the host."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_attach")


class SessionAttachExample:
    """User-owned initialization for a view entering a main Session."""

    def register(self, registrar):
        registrar.register_event_handler("session.attach", self.on_attach, {}, "user")

    async def on_attach(self, event, ops, next_handler):
        root = await ops.session.root()
        receipt = await next_handler(event)
        logger.info("session attach example: session=%s surface=%s attachment=%s root=%s accepted=%s",
                    ops.session_context.session_id, event.event_input.surface_id,
                    event.event_input.attachment_id, root, receipt.accepted)
        return receipt


def create_extension():
    return SessionAttachExample()
```

## 4. 方法契约

不新增 Ops。平台将已确认的 session_id/保存项目根绑定到 SessionContext，Core 为 Handler 创建本次 Ops。cwd 未确认可为 None；root/model 直接使用各自既有只读规则，不要求先调用 notify_context_usage，不创建模型。用户不提交“收集好的上下文”或修改平台 Adapter。

现成用户包见 [session_attach](../../examples/session_attach/README.md)，注册 on_attach、记录关联及项目根，不自动写历史或渲染页面。

## 5. 接入与兼容方案

```text
启动 Worker → bootstrap 注册 session.attach、Validator 和默认 CORE
  → 原 ops.build 准备共享 Ops → 用户包发布 on_attach

现有 Web 创建/切换目标会话，携带该 App 的稳定 view_id
  → Front / Supervisor 原路由 → Worker Server 解析原请求
  → 原 Runtime prepare + commit（保持原创建/切换交付时点）
  → foreground_changed(visible=True) 宿主观察
  → 再确认有效主会话、平台提交 channel+view 的新关联
  → Worker trigger(session.attach, SessionContext)
  → USER on_attach → 既有 Ops 或自有日志/展示 → 原样 next
  → session.attach CORE 确认关联仍有效，返回真实回执
  → 通知结束，原业务流程继续；不会新增聊天
```

类型/校验/注册在 event_types，CORE 在 event_handlers；实际 trigger 和关联表在 Server 平台通知模块。RuntimeParticipantRegistry 提供固定宿主 foreground 注册（register_host_foreground/snapshot_foreground），与 KVC 动态发布相加；Runtime 仍不知道 EventBus。其他 activity 和输入意图的参与者列表不改变；KVC 关闭时仍保留原有 disabled 语义。此入口已经有真实 view_id 提交事实，因此不新增前端请求、特殊 IPC 或 Gateway attach 解释。

本轮同 PR 增补 PR11：替换旧关联时先提交新事实，再通知旧 detach，之后仅对仍有效的新关联发 attach；显式移除和旧上下文保留见 S_15。一次切换可能包含两个分别限时的通知。

不直接使用 ui.ready，它是包槽位挂载；不使用 session.input.intent，它是输入意图；不在第一次模型调用时猜测关联。没有用量也能发 attach，没有用户包也保留真实关联和原业务。

## 6. 验收与未决问题

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原 attach/detach 合并交付为 Server !95；本次保留同一个关联服务、独立事件与文档，并与 session.end 固定删除观察者组合。前端、共享 SessionFileService 和已合入历史/用量能力沿用当前目标分支。

以下 378 项与原 real-195031 是 PR08 首次提交的历史证据；本轮 PR08/11 在同一 PR !95 交付，追加解除语义由 [F_26](F_26_session-detach.md) / [S_15](../specs/S_15_session-detach.md) 单独维护。当前联合源码的 446 项回归与 6 次 attach / 5 次 detach 真实验证见 F_26 第 6 节。

2026-10-29，Windows / Python 3.11.15，Server 基于上文固定基线并包含首次 PR08 变更，Core 为 ead700d7d18a247968d64de54918cf5d201a207a。378 项扩展、Runtime、创建/切换提交、KVC 参与者和架构回归通过，无跳过；另单独复验本特性及相关生命周期 27 项通过，包含在前述范围中、不重复累计。只有外部 Authlib 弃用提示，未运行格式化或静态检查工具。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/runtime/test_session_attach_provisioner.py tests/unit_tests/runtime/test_session_create_provisioner.py tests/unit_tests/runtime/test_session_switch_provisioner.py tests/unit_tests/runtime/test_session_provision_contract.py tests/unit_tests/runtime/test_session_lifecycle_participants.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_runtime_architecture.py --asyncio-mode=auto -o log_cli=false
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2/test_session_attach.py tests/unit_tests/runtime/test_session_attach_provisioner.py tests/unit_tests/runtime/test_session_lifecycle_participants.py --asyncio-mode=auto -o log_cli=false
```

| 场景 | 操作和预期 | 实际证据 |
| --- | --- | --- |
| 没有用量也能读取 Ops | 真用户包接收 attach，读取 root/cwd/model，原样 next 得到默认回执 | Registry/EventBus 定向测试；真实创建后首个通知早于所有 usage |
| 多视图、切换、重放 | 两视图进入同会话独立通知；同一关联重放不重复，切换再切回新 ID | 定向与真实 6 次关联记录、稳定 view_id 重连 |
| 真实提交接线 | prepare/abort 不通知，commit 后才有真实表项和 CORE 回执，重复 commit 不重放 | test_session_attach_provisioner；真实 WebSocket 创建/切换经原 IPC |
| 防改写/伪造 | 6 个输入字段、提前成功、后置修改和 int/bool 替换无效 | 参数化用户包测试核对实际调用状态中的 CORE 回执 |
| 失效关联/目标 | 关联已被替换、或 USER 期间目标目录已移除，不得取得迟到有效 CORE 回执 | 定向竞态和目标删除；真实缺失/归档目标不通知 |
| 超时与取消 | 保留已提交事实；超时任务无迟到确认，包在途计数最终释放 | 短预算构造、忽略取消和原调用取消测试 |
| KVC 共存 | 开关、suspend、lease release 不移除固定观察者，不改变输入意图列表 | 实际 KVCacheApplicationOwner 和 Registry 回归 |
| 缺失/未跟踪范围/卸载 | 无效 view、Team 不通知，未安装或卸载后原业务仍工作 | 定向测试与真实包卸载后正常切换 |

真实服务证据目录：`D:\work_space\.codex-temp\server-session-attach-event-20261029\real-195031`。安装现成示例和验收用户 ZIP，经真实 Front → Supervisor → 子 Worker 完成创建/切换；观察 6 个独立关联 ID、正确会话/视图/项目根和默认 CORE 回执。首次通知在没有任何用量与产品历史消息时发生；一个主会话可关联两个 view_id，同视图切换再切回正确记录前一目标，稳定 view_id 重连不重放。缺失/归档目标不产生 attach，原创建/切换协议保持原样。

单独发送一次真实 Qwen3.8-Flash 回合，得到有效用量；这次聊天不会多发 attach，之后新视图可正常关联。不是靠聊天或手工 notify 提供第一次上下文。未 mock Ops、模型或 Adapter，未手工构造 SessionContext。现成示例日志 6 次 accepted=True；卸载后默认切换正常。result.json、user-handler-results.jsonl、wire.jsonl、source-before/source-after 收据保存来源和一致性，cleanup.json 确认 Worker 退出和端口关闭。

边界：内存事实不跨 Worker/Runtime 重启保存，当时 view 关闭的显式撤销尚未接入，本轮 PR11 已在同 PR 增补，详见 S_15；表项不是物理在线证明。首次 PR08 不提供 viewport、surfaces Ops、detach 事件、Team/子 Agent 或产品浏览器视觉验收；当前 detach 由 F_26 独立说明。没有非 Windows 实机、远端 CI、大规模视图性能或跨重启 exactly-once 证据。2 秒仅为事件等待预算，不是整个文件校验或创建/切换的耗时上限。外部初始化副作用可能已经发生，平台不承诺回滚。

## 7. 参考资料

- [S_14](../specs/S_14_session-attach.md)、[Runtime 提交编排](../../../runtime/session_provisioner.py)、[生命周期协议](../../../runtime/session_lifecycle.py)、[Server 请求交付](../../../server/agent_ws_server.py)。
- [Web 实际 view_id 调用方](../../../channels/web/frontend/src/App.tsx)、[内置事件指南](../designs/builtin-event-development-guide.md)。
- [Claude 固定声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。

- [事件定义](../../event_types/session_attach.py)、[默认 CORE](../../event_handlers/session_attach.py)、[平台关联表与触发](../../../server/runtime/session/attach_notifications.py)、[用户包](../../examples/session_attach/README.md)。
