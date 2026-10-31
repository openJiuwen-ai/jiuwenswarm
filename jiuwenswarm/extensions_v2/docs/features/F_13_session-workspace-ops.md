# ops.session.cwd / ops.session.root 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_13 / session-workspace-ops |
| 建档日期 | 2026-10-26 |
| 后续修订 | 2026-10-29，统一使用契约、字段含义和填写示例；仅文档 |
| 关联功能 PR | PR01 [!71](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/71)，已合入 |
| 主责契约 | [S_03](../specs/S_03_session-workspace-ops.md) |

## 1. 范围、状态与依据

两项只读 Ops 已实现并合入。本次在最新 `rsi_native_agent_os` 的 7795851916dd5ce1d34b70f5a20d6d30eafc25e3 上统一说明，业务代码不变。最初解决的是宿主尚未开放目录查询的问题；该背景不是当前未完成状态。

范围是当前 Worker 中 Handler 所绑定会话的目录快照。它们不接收查询目标/路径，不根据进程目录或用户 payload 猜目录，也不触发新事件。历史与本轮文档验证分开记录在第 6 节。

## 2. 能力说明与 Claude 对照

`await ops.session.cwd()` 读取这次调用绑定的执行工作目录；`await ops.session.root()` 读取同次调用绑定的项目根目录。两者返回宿主平台的绝对路径字符串，未知为 None；读取不创建/切换目录，不检查路径此刻仍存在，不返回 Path 对象或包装字典。

Claude 固定版本的 `$.session.cwd()` / `$.session.root()` 同样提供目录读取。本地能力明确允许 None，root 只表示用户项目绑定，不能用内部任务工作区填充。对照只说明业务分工，不宣称 TypeScript 接口可直接互换。

## 3. 复用事件与上下文契约

本特性没有新增查询事件或查询 CORE。示例订阅既有 `context.usage.updated`；这只是一个已接线的业务触发入口，不是调用查询的前置操作。其他已有事件只要平台提供所需 SessionContext，其 USER Handler 同样可以调用。

| 对象 | 字段 / 来源 | 用户应如何使用 |
| --- | --- | --- |
| 原通知输入 | ContextUsageUpdate：session_id、request_id、input_tokens、output_tokens、total_tokens、cache_tokens、cache_hit_rate、context_window_tokens、usage_percent、model | 由平台汇总填写；不包含本特性查询的权威目录/模型/仓库返回值。仅用于观察和筛选，不能靠改 payload 重定向 Ops |
| 原通知输出 | ContextUsageOutput：accepted 为 bool；request_id 为 str | 默认 CORE 填 accepted=True 并回显请求 ID，仅确认输入类型已接受，不表示目录/模型/Git/UI 验收成功。用户通常原样 next 返回 |
| Ops 绑定 | Core 已有只读属性 ops.session_context | 平台随 Event 单独提供，Core 规范化/冻结后绑定到本次 Ops；不是本次新增的异步方法，用户不调用它来收集资料 |

原通知在已接线的原生流式请求末尾、有有效用量时由平台调度，Worker 内实际 trigger；不按每个 Token、工具调用或目录/模型变化触发。默认通知 CORE 只校验输入类型，没有因本次查询新增完整数值 Validator。用户保持输入并 `return await next_handler(event)` 继续原链；短路不撤销已完成回合，USER 异常依 Core 隔离规则处理。

完整通知定义以 [context_usage.py](../../event_types/context_usage.py) 为准。查询本身不会再 trigger、不会再次 ops.build，也不会把自己的返回值自动塞入原通知回执。


## 4. 方法输入输出与填写规则

### 4.1 输入：两个方法均无参数

```python
cwd = await ops.session.cwd()
project_root = await ops.session.root()
```

**用户不填写任何输入字段。**不要传 session_id、路径、项目 ID、cwd 初值或文件名。当前会话与目录由平台提供，不从用户 Event payload 里读取，也不从 Worker 的 os.getcwd() 兜底。

| 平台上下文字段 | 类型 / 缺省 | 准确含义、由谁填写 | 示例与权限 |
| --- | --- | --- | --- |
| SessionContext.session_id | str，会话绑定时提供 | 这次事件的会话关联身份；用户不靠修改 payload 更换它 | session-a；只读 |
| SessionContext.cwd | str 或 None，未提供为 None | 原生执行任务/平台入口在事件边界采集的工作目录，不是整个 Worker 的进程目录 | Windows 的 D:/projects/repo/app/src；只读 |
| SessionContext.metadata.project_root | str 或缺省 | 平台确认绑定的用户项目根目录；没有绑定则缺失/None | D:/projects/repo/app；只读，不自动补成 cwd |

平台需使用本机绝对路径语法：Windows 如 D:/projects/repo/app/src，POSIX 如 /home/me/repo/app/src。Ops 不展开 `~`、环境变量或相对路径，也不在读取时 normalize/resolve/修复原路径。

### 4.2 返回：整体就是字符串或 None

| 返回项 | 类型 / 何时出现 | 准确含义、示例与消费方式 |
| --- | --- | --- |
| cwd() 的非 None 返回 | str | 本次快照的工作目录，如 D:/projects/repo/app/src；不是 {'cwd':...}，直接使用字符串 |
| root() 的非 None 返回 | str | 本次快照的项目根，如 D:/projects/repo/app；不是 Git 根、内部 Agent workspace 或临时目录 |
| None | 该项未提供 | 表示当前上下文没有该目录资料；不证明会话不存在或磁盘目录不存在，不回退为 '.' / 空字符串 |

例如 cwd=repo/app/src、root=repo/app，而 repo() 可返回 repo。三者可以不同；无项目会话仍可能有 cwd、root=None。若事件只提供 session_id，两项都可能为 None。两次读取同一调用中的目录是同一事件边界的事实，不是持续跟踪外部 chdir 的实时视图。

### 4.3 错误与缺失不要混用

| 情况 | 能力函数行为 | 用户应如何处理 |
| --- | --- | --- |
| 没有 SessionContext | RuntimeError | 选择平台已绑定会话的事件接入；不手工猜会话 |
| 目录为 None | 返回 None | 显示未知或跳过需要目录的业务 |
| 目录不是 str/None | TypeError | 属于平台资料格式错误，不冒充“无项目” |
| 空字符串或非本机绝对路径 | ValueError | 不用相对路径补救为另一目录 |

经 EventBus 时，不合法上下文也可能先被 Core 拒绝。USER 中未捕获异常按原事件的故障隔离处理；原回合继续不等于本次查询成功。

## 5. 平台接入与用户使用

```text
启动：Worker 装配 → ops.build → CORE 返回 Builder
  → session_ops_build BUILTIN 在 next 返回后登记共享 session 方法
  → Core 保存能力目录；Registry 发布用户 Handler

业务：已接线的平台入口收集真实 SessionContext
  → Worker EventBus.trigger(原事件, context)
  → Core 为实际 Handler 创建当次 Ops，绑定会话和包身份
  → 用户 Handler await 本特性只读 Ops
  → Core 方法代理自动把当前 Ops 传给对应平台函数
  → 平台读取可信快照或本特性明确允许的数据源 → 直接返回值
  → 用户自行记录/展示，再 next_handler(原事件)
  → 剩余原事件 Handler → 原 CORE → 原通知任务收到回执
```

启动时准备的是共享方法目录，不执行查询。业务时创建绑定本次会话/包身份的 Ops 视图，不按事件名只创建少数方法；用户实际 await 哪个方法才执行哪个查询。这里的“Core 方法代理”是框架适配，不是新建的业务 CORE。

USER 负责自己的日志或展示；BUILTIN 负责启动时登记 Ops；原事件 CORE 处理原事件。三个查询特性均不新增专用查询 CORE、Rail、Front/Supervisor 协议。EventBus 仍只由 Worker 持有。

用户提供 manifest.yaml / extension.py，通过既有管理入口安装包并进行正常业务操作。平台负责业务触发和可信资料，用户不修改 Adapter，不创建 EventBus/Builder，不调用 notify 来“取回上下文”。安装成功只说明 Handler 已发布；尚无对应业务事件时不会自动执行查询。

返回值交给调用方，不自动缓存查询结果、写第二份状态文件或渲染 UI。示例的 logger 是用户显式写日志；其他使用方式需用户接好自己的消费逻辑。卸载移除 USER Handler，不删除平台能力或既有会话事实。


### 5.1 可复制的用户包

PR01 没有另附仓库示例目录；用户可把下面两个文件放在同一新目录并打成 ZIP。日志是显式消费结果，不代表自动前端展示。

`extension.py`：

```python
import logging

logger = logging.getLogger(__name__)

class WorkspaceInfo:
    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        cwd = await ops.session.cwd()
        root = await ops.session.root()
        logger.info("workspace: cwd=%s root=%s", cwd, root)
        return await next_handler(event)

def create_extension():
    return WorkspaceInfo()
```

`manifest.yaml`：

```yaml
schemaVersion: 1
package:
  id: example.session-workspace-info
  version: 1.0.0
  displayName: Session Workspace Info
extensions:
  - id: workspace-reader
    runtime: backend
    module: extension.py
```

将两文件放在 ZIP 根目录，在服务可读路径使用 `/extension install "D:/extensions/session-workspace-info.zip"`。正常消息产生有效原生用量通知后，USER 日志可见 cwd/root；其他已有会话绑定事件同样可调用。原生入口会提供真实快照，但本特性不承诺所有未接线入口都有目录资料。

## 6. 验收与未决问题

2026-10-29 本轮只修改文档，不重跑历史业务/模型验收。已核对无参签名、目录读取与错误、示例注册/调用/next、完整链路和本地引用；验证记录：`D:\work_space\.codex-temp\server-session-docs-20261029\documentation-verification.json`。

<a id="5-验证设计执行记录与结论"></a>

### 6.1 原功能验证（2026-10-26，保留原结果）

| 场景 | 验收要求 | 实际结果 |
| --- | --- | --- |
| V-01 | 经生产 bootstrap 与 ops.build，Handler 可同时调用两个 Ops，并保留原 ui 能力 | 通过：真实包装载用例和独立进程验证 |
| V-02 | cwd 为项目子目录时，cwd 与 root 返回不同的绝对路径 | 通过：`project/src` 与 `project` 分别返回 |
| V-03 | 无会话上下文报错；缺少目录资料返回 None；相对或非法路径报错 | 通过：能力边界用例及无目录快照用例 |
| V-04 | 两个并发会话、嵌套事件各自保持自己的路径，不使用事件输入自报路径 | 通过：并发、嵌套及事件输入伪造路径用例 |
| V-05 | 用量发布入口在调度前固定路径，空项目绑定不伪造 root | 通过：发布后修改任务 CWD 不改变已发布快照；旧调用兼容 |
| V-06 | 真实扩展包安装、激活、调用、卸载均通过，查询不创建目录或启动模型 | 通过：生产装配、Registry、真实后端包、独立进程与清理检查 |
| V-07 | 既有 Extensions V2 与相关宿主测试回归通过 | V2 115 项、原生用量 3 项及无项目工作区 62 项通过；外围 11 项 Windows 失败在未修改基线全部复现，详见下文 |

执行日期为 2026-10-26，环境为 Windows、Python 3.11.15 隔离环境，Core 源码使用 `rsi_native_agent_os` 的 `ead700d7d` 快照，未修改 Core。通过 `PYTHONPATH` 核对两仓源码来源后运行：

```powershell
python -m pytest tests/unit_tests/extensions_v2 `
  tests/unit_tests/agentserver/test_context_usage.py `
  tests/unit_tests/agentserver/test_workspace_paths.py `
  tests/unit_tests/agentserver/test_projectless_agent_workspace.py `
  tests/unit_tests/agentserver/test_auto_permission_workspace_owner.py `
  -o addopts='' -o asyncio_mode=auto -o log_cli=false -q
```

结果为 225 项通过、11 项失败。失败集中在原有路径脱敏、路径分隔符及 Windows 大小写比较；在未修改的 `9e9539dde` 基线上用相同 Core 和解释器复跑两个失败文件，得到 45 项通过、相同的 11 项失败，失败用例集合完全一致。本次未修订这些外围行为，也不将整轮回归表述为全绿。

独立进程验证使用隔离配置目录和生产 `install_extensions()`，经真实管理门面加载临时后端包，订阅现有用量事件并调用两个 Ops。两个项目会话分别得到自己的子目录与项目根；无项目会话保留 cwd、root 为 None；缺少路径的旧发布调用两个值均为 None。卸载后用户 Handler 不再执行；停机、日志关闭与临时目录清理通过，进程退出码为 0。此验证未使用模型和网络传输，不等同于真实聊天或浏览器联调。

已核对 `handler_ops/` 归属、生产 bootstrap 接线、唯一一次 Ops Build、公开导入、文档编号和引用；按仓库约束未运行格式化或静态检查工具。当前进度和验收依据统一维护在关联开发计划中。

### 6.2 当前边界

未提供可信目录的入口需由该平台入口维护者接线；注册 USER Handler 本身不自动收集目录。目录读取不代表目录存在、授权到所有文件、完整恢复任务或浏览器已展示。原历史验证的替身/真实进程/网络边界保留原记录，本轮不扩大解释。

## 7. 参考资料

- [S_03](../specs/S_03_session-workspace-ops.md)、[session_ops.py](../../handler_ops/session_ops.py)、[通知定义与平台触发](../../event_types/context_usage.py)。
- [PR02 主模型](F_14_session-model-ops.md)、[PR03 Git 工作树](F_15_session-repo-ops.md)：目录身份与模型/Git 事实不同。
- [内置事件接入说明](../designs/builtin-event-development-guide.md)、[Claude 固定版本声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。
