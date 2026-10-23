# 浏览器 Jev 配置

在「设置 > 浏览器服务 > Jev 模型配置」中点击「配置」，与模型配置一样集中编辑并保存：

| 提供商 | API 地址 | 默认模型 |
| --- | --- | --- |
| OpenRouter | 内置 `https://openrouter.ai/api/alpha` | `typesafe/jev-1.13` |
| TypeSafe | 内置 `https://api.typesafe.ai/v1` | `jev-1.13.0` |
| 自定义 | 必须填写 HTTPS 基础地址 | `jev-1.13.0`，可修改 |

表单只包含提供商、API 密钥、模型，以及自定义提供商的 API 地址。
已有密钥自动回填，默认掩码显示，可点击可见性按钮查看；未配置时为空。
取消不会保存。切换提供商会使用对应模板和独立密钥，填写完成后点击保存。

自定义服务须兼容 TypeSafe Jev `/systemone` 接口，填写不带 `/systemone` 的基础地址；
它不是普通 Chat Completions 服务。OpenRouter 则使用其原生 Jev `/decisions` 接口。
不提供协议、上下文窗口或自定义名称。

Jev 模式（LLM / Shadow / Hybrid）保留在浏览器设置中，保存模型配置不会自动启用 Jev。
清除当前密钥时先切回 LLM，不影响其他提供商保存的密钥。
Chrome 路径、显示模式、浏览器登录态和生命周期不受本次表单调整影响。
