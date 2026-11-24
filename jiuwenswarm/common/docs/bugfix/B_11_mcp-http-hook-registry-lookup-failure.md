# B_11 — MCP HTTP 出口钩子在 registry 查询异常时打断 MCP 调用

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-24 |
| 严重程度 | 低（仅 `get_instance()` 抛 `RuntimeError` 以外异常的路径；正常"未初始化"仍返回空头） |
| 范围 | `jiuwenswarm/common/mcp_http_hook.py`、`tests/unit_tests/common/test_mcp_http_hook.py` |
| 测试基线 | `test_mcp_http_hook.py` 24 passed |
| 关联 spec | `S_09_mcp-integration.md`（N25 契约不变；本修复落实其 `Never raises` 语义） |

## 现象

`apply_mcp_http_hook` 的 docstring 与 S_09 N25 要求：扩展框架不可用 / 无订阅者 /
handler 抛错 / 框架未初始化时都回落到静态配置头，**不得让 MCP 调用失败**。但
`ExtensionRegistry.get_instance()` 的异常捕获只写了 `except RuntimeError`。

## 根因

函数内其它异常源（扩展框架 import、`registry.trigger`、`_sanitize_headers`）都用
`except Exception` 兜底，唯独 registry 查询只兜"未初始化"这一种。若该查询因配置损坏 /
扩展装载失败等抛出 `ImportError` / `AttributeError` 等异常，会穿过 hook，进入
`_hooked_async_auth_flow` 生成器并打断 httpx auth flow，最终让 MCP 调用失败——与
`Never raises` 的契约矛盾。

## 决策

把该处收窄的 `except RuntimeError` 改为 `except Exception`，与同函数其它异常源的容错口径
一致；异常按既有语义回落 `{}`（静态配置头照常发送）。契约不变，只是把契约覆盖到所有
registry 查询异常。

## 拒绝的方案

- **在 `ExtensionRegistry.get_instance()` 内吞异常**：该单例的调用方普遍依赖
  `RuntimeError` 表达"未初始化"，在源头改语义会扩张影响面，且不利于排障。
- **按异常类型逐个枚举**：无法穷举第三方扩展 / 装载链可能抛出的异常，仍会漏。
- **直接删除 docstring 的 Never raises 承诺**：MCP 鉴权钩子是外挂能力，任何失败都不应
  打断主调用链，契约本身是对的。

## 验证

- 新增参数化用例 `test_registry_lookup_failure_never_breaks_mcp_call`：mock
  `get_instance` 分别抛 `RuntimeError` / `ImportError` / `AttributeError`，断言 hook 返回 `{}`
  且不传播异常。
- 回归：`tests/unit_tests/common/test_mcp_http_hook.py` 24 passed。

## 已知遗留

- 无。该修复仅扩大异常兜底面，不改变正常路径的头合并、订阅者判定与请求归属语义。
