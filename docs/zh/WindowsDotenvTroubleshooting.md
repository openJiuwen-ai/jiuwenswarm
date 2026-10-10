# Windows dotenv 首行配置失效排查

## 问题与修复范围

Windows PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会写入 UTF-8 BOM。如果用普通 UTF-8 解析 `.env`，首个键可能变为带 U+FEFF 前缀的名称，而非预期的 `API_BASE` 或能力开关。这会造成首行配置未生效；多模态迁移还可能把显式关闭的能力误判为未配置。

运行时 dotenv 加载、命名实例 bootstrap 加载和多模态能力迁移现在按 `utf-8-sig` 读取，同时兼容无 BOM 的普通 UTF-8。该修改不改变原有 override 策略、端口保护或插值语义，也不支持 UTF-16 文件。其他直接使用 dotenv 的独立入口不在本补丁范围内。

## 不暴露凭据的离线检查

新的 `runtime-config-check` Skill 提供一个只读 helper：

```powershell
python jiuwenswarm/resources/agent/workspace/skills/runtime-config-check/scripts/check_env.py --dotenv ./config.env
```

脚本只检查所指定文件的三项基础模型配置：`API_BASE`、`API_KEY` 和 `MODEL_NAME`，不会请求模型、读取其他文件、修改配置或打印配置值。它不计算进程最终配置，也不能验证模型权限、网络连通性和 Token 有效性。退出码 0 表示基础格式检查通过，1 表示配置问题，2 表示文件读取或解析失败。

如果尚未安装 python-dotenv，请使用项目环境运行上述脚本。浏览器登录成功不代表 Git 推送已经认证，也不代表模型接口已配置。

## PowerShell 路径示例

```powershell
$cfg = Join-Path $env:USERPROFILE '.jiuwenswarm/config/.env'
python jiuwenswarm/resources/agent/workspace/skills/runtime-config-check/scripts/check_env.py --dotenv $cfg
```

不要把 Markdown 链接、加粗标记或说明文字写进 URL 值。API 地址应是原始 HTTP(S) URL。公开问题报告只需要脚本的脱敏输出、软件版本和已观察到的错误类别；不要附完整 `.env`、API Key 或账户密码。
