# 网页与搜索请求未受配置 key 对应的实际连接检查约束

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-29 |
| 严重程度 | 高：开启 W3 时，工具入口判定不等于实际连接受控 |
| 范围 | common/http_proxy_config.py 与 AgentServer 网页工具 |
| 测试基线 | 本地 HTTP/TLS、代理兼容、缓存与 Jina 定向回归 |
| 关联 spec | S_08_config-and-environment.md，N11 |

## 现象与根因

原 requests 路径直接连接，并在 ProxyError 后去掉代理重试。工具入口判定的域名
无法约束后续 DNS 选择、重定向或 Jina 二次请求；缓存命中也未检查规范化后的目标。

## 决策

仅在工具执行绑定 NetGuard key 时，接入 agent-core 的固定地址传输。完整 DNS 快照、
实际选用地址与 URL 一起按 key 判定，每跳重查；保留 Host/SNI、强制 TLS 验证和自定义
CA 路径。缓存命中前检查规范化 URL，缓存记录的 URL 不同时再次检查。
Jina 和搜索复用已有 common 请求入口，继承同一 key。未绑定的原路径保持兼容。

## 拒绝的方案

不使用调用侧临时配置/引擎；不只检查首次 URL 后交给客户端自动解析和重定向；
不在代理失败后直连。当前不实现新的代理 CONNECT 协议，受保护路径显式拒绝代理。

## 验证与已知遗留

核心回归包含真实本地 HTTP/TLS 和证书主机名不匹配拒绝；AgentServer 用例包含
缓存、Jina 二次请求及旧的无上下文代理行为。尚未进行部署环境 E2E。
沙箱、Gateway A2A manager、模型和 MCP 传输不在本次范围。
