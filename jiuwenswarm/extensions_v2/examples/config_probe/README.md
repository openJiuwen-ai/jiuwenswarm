# 配置扩展验证包

本包验证 `config.describe`、`config.set` 和三个 `ops.config.*`，运行在 AgentServer。

## 平台与本包分别做什么

平台通过已有 `ops.build` 登记 `value`、`items`、`snapshot`，从公开配置目录读取原生配置。
`items()` 逐项触发 `config.describe`；原生配置保存入口触发 `config.set`，默认 CORE 继续原保存及热更新。
本包只订阅 USER Handler：调整 RSI 的展示说明、拒绝开启 RSI，并增加 `/config_probe` 命令。
本包不创建 EventBus/SessionContext，不替换平台 CORE，不保存另一份配置，不包含前端组件。

## 安装与验证

将本目录的四个文件打包到 ZIP 根目录，通过受支持的单 Agent 会话安装：

```text
/extension install "<本机绝对路径>/config-probe.zip"
/config_probe
```

命令返回 `value`、`items`、`snapshot` 三项。RSI 行名称为 `RSI (config probe)`；
`snapshot.rsi.enabled` 是当前原生值。它是受控公开投影，不包含模型密钥或任意 YAML 字段。

在原有设置入口尝试开启 RSI，或者由现有 `config.set` 请求提交 `{"rsi_enabled":true}`：
预期请求被拒绝，返回 `CONFIG_SET_DENIED`，配置文件不发生本次写入。关闭 RSI 仍可沿默认链保存。
此演示策略对当前宿主内匹配的请求生效；验证完成后卸载：

```text
/extension uninstall example.config-probe
```

卸载后自定义命令和策略消失，平台配置读取与原保存能力保留。
自动验收使用隔离配置文件、真实 ZIP 安装、命令入口和 ConfigAdapter，独立读回 YAML；不需要模型或 UI 渲染。

## 范围

首批字段为 `preferred_language`、`react.context_engine_config.enabled`、`rsi.enabled`、`rea.enabled`。
读作用域支持 `effective` 与 `user`，不支持 `project`/`session`。企业版语言由原 Gateway/用户偏好服务负责，
不作为本宿主 YAML 的公开字段。三个 Ops 都可在无会话的合法扩展 Handler 中调用。
