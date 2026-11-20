# server.run_command 规约

最近修订：2026-11-04。字段、示例、触发覆盖及证据维护在 [F_30](../features/F_30_server-run-command.md)。

## 1. 范围和契约

事件沿用项目名 server.run_command，对照 Claude command.run。当前接入 Worker DeepAdapter 的文本命令入口；Gateway 自行消费的命令和独立 command.* RPC 不在该入口内。插件 Ops 方法与命令目录不属于本次接口。

输入为 ServerRunCommandInput(command, args, origin, presentation)，输出为 ServerRunCommandResult(text=None, context=None, ref=None)。Python 通过冻结 dataclass 和 tuple 适配 Claude 对象与数组。

## 2. 不变量

| 编号 | 必须成立的行为 |
| --- | --- |
| CMD-01 | 平台先绑定真实会话、命令和来源，再触发事件；私有原生函数、Task 和完整结果不进入事件输入 |
| CMD-02 | 仅 args 可改写；command/origin 固定，presentation 省略时继承，改变时拒绝 |
| CMD-03 | USER 不调用 next 可接管命令，原生执行次数为 0；合法文字结果表达主动拒绝 |
| CMD-04 | CORE 在本次 Task/会话内最多执行一次原生函数，重复 next 复用结果或异常 |
| CMD-05 | ref 只能指向本次真实原生结果，携带 ref 时不得伪造原生文字；替换文字不能撤销已发生的副作用 |
| CMD-06 | context 每项为一条非空的隐藏用户消息；省略继承，显式返回保留完整原前缀；最终一次提交给模型上下文 |
| CMD-07 | 多 USER 的备注继承后仍对外层 next 可见；Session 身份、原生审批和 follow-up 事实不因改写文字丢失 |
| CMD-08 | 缺宿主帧的直接触发、跨 Task 借用及同会话重入不能执行受保护的原生 continuation |
| CMD-09 | 异常/取消关闭调用帧，不自动重试或回滚命令；备注提交失败不得伪装成功 |
| CMD-10 | 输入与输出校验、USER 故障隔离遵守既有 EventBus；非法 USER 返回不能作为可靠拒绝方式 |

## 3. 生效与生命周期

bootstrap 注册唯一 CORE，用户包通过 registrar 注册/卸载 Handler。没有新增本事件 BUILTIN。Worker 没有终端测量时展示事实为 false/80；未知命令未被用户接管时明确拒绝。备注通过活跃或恢复的 Session 和 ContextEngine 保存，删除插件不会删除既有模型备注。

## 4. 验证入口

[事件与真实安装包测试](../../../../tests/unit_tests/extensions_v2/test_server_run_command.py)、[适配与真实 Core 上下文测试](../../../../tests/unit_tests/extensions_v2/test_server_run_command_adapter.py)。测试与独立进程证据的具体范围见 F_30 第 6 节。
