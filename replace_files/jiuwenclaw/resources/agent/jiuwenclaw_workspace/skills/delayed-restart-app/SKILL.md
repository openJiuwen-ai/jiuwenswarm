---
name: delayed-restart-app
description: 安排延迟重启本 Agent 所在的服务（jiuwenclaw app）。执行后当前 Agent 进程会被终止并重新启动，当前会话会断开。用于用户要求重启、配置更新需生效、或服务异常需重载时。使用 bash 调用脚本。
---

# 重启本 Agent 所在的服务

本 skill 会**重启 Agent 自身所在的服务进程**（jiuwenclaw app）。执行后 Agent 进程将被终止并重新拉起，当前会话会断开，新连接将连到新进程。

当用户要求「重启服务」「重启 app」「重启 Agent」「配置已更新需重启」或类似需求时，使用 `bash` 执行本 skill 下的脚本。

## 脚本位置

本 skill 目录下包含 `launch_delayed_restart.py`，以 detached 方式启动 `jiuwenclaw.scripts.delayed_restart_app`。

## 执行命令

使用 `bash` 工具执行（必须使用 launcher，否则重启时会连同脚本一起被终止）：

```bash
python %USERPROFILE%\.jiuwenclaw\agent\skills\delayed-restart-app\launch_delayed_restart.py --pid <当前 app 的 PID> --delay 5
```

（Unix/macOS 使用：`python ~/.jiuwenclaw/agent/skills/delayed-restart-app/launch_delayed_restart.py --pid <PID> --delay 5`）

- `--pid`：必填，当前 jiuwenclaw app 进程的 PID（执行前需先获取，如从 config 或通过 `tasklist`/`pgrep` 等命令）
- `--delay 5`：延迟 5 秒后重启（可改为 3、10 等）

## When to Use

- 用户明确要求「重启 app」「重启服务」「重启 Agent」
- 配置已通过 config.set 等修改，用户询问或要求重启以生效
- 用户反馈服务异常，建议重启

## 注意事项

- 重启后当前会话会断开，新连接将使用新进程
- 默认 5 秒延迟，便于先返回响应再重启
