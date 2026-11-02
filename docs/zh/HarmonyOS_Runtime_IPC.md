# HarmonyOS Runtime IPC 测试

AgentServer 在鸿蒙运行环境默认使用 `front_loopback`：Front 在 `127.0.0.1` 分配临时端口，Worker 主动连接并认证。
Worker 不绑定 Unix socket，不获取 resident Worker 锁；Front 断开后 Worker 清理 Runtime 并退出。
普通 Linux/macOS 继续默认使用 Unix socket、Windows 继续默认使用 Named Pipe。

## 默认行为与手动覆盖

使用本分支代码更新鸿蒙安装包后，按原有方式启动即可，无需再手动设置 `JIUWENSWARM_RUNTIME_IPC`。
鸿蒙识别沿用现有规则：`JIUWEN_HARMONY_RUNTIME=1`、`sys.platform == "ohos"`，或 PATH 中存在同时包含 `/data/app/` 和 `/hnp/` 的目录。
因此，Python 报告为 Linux 的 HNP 沙箱也能默认启用新 transport。

仅在普通桌面平台手动测试时，需要显式选择：

```powershell
$env:JIUWENSWARM_RUNTIME_IPC = 'front_loopback'
jiuwenswarm-agentserver
```

显式设置 `JIUWENSWARM_RUNTIME_IPC=resident` 可以覆盖鸿蒙默认值；未设置、空值或纯空格使用平台默认值。未知值仍会报错。
失败时查看 `runtime_ipc=front_loopback`、阶段和 errno。实现不会静默回退到 Unix socket。
地址和一次性 token 通过 Worker stdin 传递，不需要配置固定 IPC 端口，也不要把 token 加到命令行或日志。

## 本地验证

在已安装测试依赖的仓库环境执行：

```text
python -m pytest tests/unit_tests/agentserver/test_front_loopback_ipc.py --no-cov
```

用例包含非法认证、慢连接、超时、启动失败与取消、真实 Worker 子进程启动/重启/退出，以及真实 Front 被强制结束后的 Worker 退出。
请求/推送和 dispatcher drain 使用确定性 Runtime 替身；测试不调用真实模型。
现有业务协议和 32 MB 业务帧上限保持不变。

## 鸿蒙打包与验收

主仓测试分支提供 transport 实现。鸿蒙打包仓仍须更新到该提交，并同时重建 `python-jiuwenswarm.hnp` 和 `pyc_bundle.tar`，防止旧 `.py_src` 覆盖新实现。
随后构建、注入、签名 HAP，记录包内版本，并在真机验证至少 20 次冷启动、Worker 崩溃恢复、Front 强退、请求/流式返回/取消和正常关闭。
每次确认 Worker 实际退出、端口已释放且没有 Runtime socket 文件。
本仓 Windows 测试不能替代 HarmonyOS 真机、POSIX 或 HNP/HAP 验证。

回滚时成套恢复业务基线、HNP 和覆盖层，并移除 `JIUWENSWARM_RUNTIME_IPC` 配置。

## 0.2.8 适配边界与验证

本实现独立基于主仓 `0.2.8@df3b123ffd887358749b7fe82f1d3992a41d5197`，未整体合入 develop。
保留 IPC v1 和 32 MB 业务帧上限。仅 attached 路径增加 stdin 私有启动载荷、代码内容摘要/数据范围验证、Hello 身份字段及 drain 后的 shutdown.done；Windows resident 路径不读取该载荷，也不发送新增回执。
没有移植 develop 的 v2 journal、重放、身份缓存或 resident election。

2026-11-02 在 Windows CPython 3.11.15、独立锁定依赖环境执行：

- 最终新 IPC 专项与真实进程用例：75 passed，包含启动清理失败发布 FAILED 的用例。
- 原四文件定向回归：151 passed、1 skipped。
- 扩大回归：82 passed、5 skipped、4 failed、3 fixture errors。四个失败在未修改的同一基线上逐项复现；原入口 fixture 在没有当前 event loop 的条件下也复现相同错误。这些既有失败不算通过，完整结果保留在 PR。
- 最终源码构建 wheel 并隔离安装，从源码目录外验证默认 Windows Named Pipe、模拟鸿蒙 loopback 的请求与真实 Worker PID 退出，两种模式均通过；不是 HarmonyOS 真机验证。
