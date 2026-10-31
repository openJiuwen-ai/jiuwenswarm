# MCP 包装守卫重置后可叠加包装，锁定 SDK 仍存在取消与清理兼容缺口

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-30 |
| 范围 | mcp_call_timeout_patch.py，timeout/lifecycle/isolation 测试 |
| 严重程度 | 高：递归 I/O 锁等待及旧 SDK 清理可导致测试或请求挂起 |
| SDK 基线 | uv.lock 中的 agent-core 3460150aa5f319b0b8f84a4ed10eec0a08b0feb1 |
| 关联 spec | S_09_mcp-integration.md，N7 |

## 证据与根因

清空 `_PATCHED` 和注册集合后再次 apply，会在仍有包装的方法/factory 上叠加一层。
新增的连续重装回归在第二次安装即发现函数身份改变；内外层共享不可重入 I/O 锁，
会形成相互等待。fixture 的状态恢复不能替代实际绑定的幂等性检查。

本轮将 SDK 导出到临时目录，用 PYTHONPATH 对齐 uv.lock 中的提交，未修改 SDK 仓库。
同时发现两个独立问题：旧 HTTP connect 捕获 BaseException 并返回 False，吞掉外部
取消和超时取消；旧 SSE `_do_disconnect` 用 wait_for 执行 __aexit__，在 Python 3.11
中跨任务退出 AnyIO scope。仅添加包装标记不足以使锁定 SDK 的生命周期测试通过。

## 决策

- 对 call/list、disconnect、connect read-timeout、传输 factory 和 ToolMgr factory
  分别设置 `_jws_mcp_wrapper` 标记。重复安装检查当前绑定，注册集合仅记录状态。
  已包装方法保持原函数身份；已被替换的方法即使注册集合未变，也能正确重新包装。
- 两个 fixture 均取消依赖 `_PATCHED` 的提前返回，继续成对快照与恢复方法和标记。
- 建连返回后检查本次新增的取消请求，恢复旧 SDK 吞掉的取消，保留外层超时语义。
- SSE 沿用 SDK owner 和 AsyncExitStack，在所属任务直接有界 aclose。失败时返回
  False 使 SDK public disconnect 仍完成 owner 停止，外层继续将 False 报错；保留
  失败标记和资源引用，禁止重复关闭已消费的 stack 或复用失败连接。
- 清理预算测试兼容旧 SDK 在取消后完成整个关闭的情况：有残留资源必须拒绝重连；
  无残留则验证重连及关闭成功。所有等待有界，不依赖 CPU 百分比或严格耗时断言。

## 拒绝的方案

不只在 fixture 中增加另一个全局布尔守卫，不提高超时或跳过挂起测试，不通过修改
AnyIO 私有状态或升级 SDK 掩盖锁定版本的问题。保留既有 owner 结构，不引入新框架。

## 验证

覆盖连续重装 10 次、原始 SDK 方法被替换后的重装、冷启动/预装补丁两种状态、文件
正序/倒序四种组合，并检查结束后的方法绑定与 guard 精确恢复。生命周期测试继续
检查进入/退出任务相同、后台任务结束、取消回调停止增长，以及失败关闭不会重复执行。

SDK 对齐 uv.lock 后，Python 3.11 双进程和 Python 3.12 的 393 项 MCP 回归均通过。
独立生命周期文件与多文件组合均运行完成。新增测试完整 Ruff、修改模块致命错误检查
及 git diff --check 通过。

## 边界

这修复了已复现的重复包装与锁定 SDK 兼容问题，不代表远端 CI 已重新通过，也不证明
模型断流或专家团长耗时已解决。拒绝响应取消的底层协程仍不能被 Python 强制终止。
