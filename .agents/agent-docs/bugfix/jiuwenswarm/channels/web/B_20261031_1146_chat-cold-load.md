---
id: B_20261031_1146_chat-cold-load
title: 会话嵌套路由冷加载白屏修复
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/channels/web
files:
  - jiuwenswarm/channels/web/app_web.py
  - jiuwenswarm/channels/web/frontend/vite.config.ts
  - tests/unit_tests/channels/web/test_web_static_cache_headers.py
designs: []
design_note: 对已有静态服务契约的局部修复，与 nginx 的资源别名保持兼容，无独立设计。
---

# 会话嵌套路由冷加载白屏修复

## 现象与根因

基线为 develop 的 eabe143db。直接打开或刷新 `/chat/new`、`/chat/<session_id>`，
构建入口中的 `./assets/main-*.js` 被解析成 `/chat/assets/main-*.js`。
Python 静态服务找不到该路径，却回退返回 200 HTML，导致浏览器拒绝执行模块、应用无法挂载。

0722806d2 将 base 从 `ELECTRON ? './' : '/'` 改为统一 `./`，以适配 Manager 的 `/chat/` 挂载。
无条件 SPA fallback 在该提交之前已存在；该提交新增的是运行时配置注入。
eabe143db 没有修改这些静态服务文件。run-now 内部导航增加了嵌套 URL 的使用，
风险主要在后续刷新或直接打开该链接。

## 当前契约与修复决策

保留相对 base 和运行时配置注入。现有 `docker/web.nginx.conf.template` 已支持
嵌套路由的 `/assets/` 别名，本次补齐 Python 服务：原路径不存在时，限定在 dist 的
`assets/` 目录中查找同一资源，使用规范资源路径决定 MIME、immutable 缓存及条件请求。
路径解析同时校验 dist 与 assets 边界，不允许别名通过 `..` 跳出资源目录。

缺失的 assets 路径和常见静态扩展名返回 404；无扩展名的客户端路由继续回退入口 HTML。
已有文件优先匹配。桌面入口 Cookie 检查和现有静态资源访问规则保持原有语义。

## 拒绝的方案

- 仅将不存在的资源改为 404：无法修复主模块地址错误，白屏仍会发生。
- 统一恢复绝对 base `/`：会重新请求 Manager 自身的 `/assets/`，破坏子路径挂载。
- 按任意文件名逐级剥离路径：扩大了别名范围；本次仅为 Vite 的 assets 目录提供兼容。

## 回归证据与适用范围

环境为 Windows、项目 `.venv` Python 3.11.15。仅运行以下定向测试，未运行格式化或静态检查：

```powershell
$env:PYTHONPATH='.'
.venv/Scripts/python.exe -m pytest tests/unit_tests/channels/web/test_web_static_cache_headers.py tests/unit_tests/channels/web/test_web_file_api_errors.py -q --no-cov
```

结果：33 passed。新增用例在修复前为 10 failed、5 passed（仅静态服务文件）；
修复后覆盖根路径、`/chat/`、新会话、已有会话、多层路由、JS/CSS、worker、
immutable/304、查询参数、缺失资源及编码路径越界。

另用真实 `_SpaStaticHandler` 和现有 dist 验证 `/chat/new`、`/chat/web_probe`，
以及模拟 Manager 剥离 `/chat` 前缀后的 `/chat/`、`/chat/chat/web_probe`：
每组入口引用的 4 个实际构建资源均为 200 且 MIME 非 HTML。
这是 HTTP 资源验证；未执行浏览器视觉验证、真实 Manager 联调或 macOS/Linux 回归。
修改资源映射、构建 base、Manager 代理规则时需重新验证。

## 资料同步与遗留

同步中英文 Quickstart 与 Vite 配置注释。本模块无单独的现行静态挂载 spec，
本次按实际 Python/nginx 实现和 HTTP 行为测试核对契约。
本次未处理嵌套路由下根级 public 文件（例如相对 favicon）的别名；不影响主应用模块加载。
