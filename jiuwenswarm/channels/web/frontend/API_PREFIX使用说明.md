# 接口前缀（VITE_API_PREFIX）使用说明

## 1. 背景

前端可部署在统一门户 / 网关的任意路径前缀下（如 `/jiuwenswarm/clawmanagerweb/chat/`）。通过两个环境变量控制：

| 环境变量 | 作用 | 配置位置 |
|---|---|---|
| `VITE_BASE_PATH` | 页面基础路径（构建 `base`，决定资源与路由前缀） | `.env.development` / `.env.production`，或构建时注入 |
| `VITE_API_PREFIX` | 接口前缀（同源相对请求自动拼接） | 同上 |

未配置 `VITE_API_PREFIX` 时，所有行为与原先完全一致，本功能对默认部署形态无影响。

## 2. 新增接口时必须做的事（核心规则）

**新增一个发给后端的请求时，先判断它走哪种通道，再按对应规则处理。**

### 2.1 走 `window.fetch` 的同源相对路径请求 —— 自动加前缀，无需处理

`main.tsx` 安装了全局 fetch 拦截器（`installApiPrefixFetchInterceptor`），会自动为指向同源的相对路径请求拼接 `VITE_API_PREFIX`：

- 支持 `string` / `URL` / `Request` 三种传参形式；
- 幂等：路径已带前缀时不会重复拼接；
- 跨源请求（`http(s)://`、`//`、`data:`、`blob:`）不受影响。

### 2.2 不走 `fetch` 的请求 —— 必须手动加前缀

以下通道**不会**自动加前缀，新增时必须手动处理：

- **WebSocket 连接**：统一使用 `getWsBase()` 生成地址（已内置前缀推导：页面 origin + 前缀 + `/ws`）。禁止手写 `new WebSocket('/xxx')` 这类不带前缀的相对地址；
- **图片 / 音视频 / 文件下载等资源地址**：统一调用 `resolveApiUrl(path)`（`src/utils/env.ts`），参考 `filePreviewModel.ts`、`MediaRenderer.tsx` 中的既有做法；
- **手动构造 URL / 相对地址写入标签属性等场景**：同样调用 `resolveApiUrl()`，或自行用 `getApiPrefix()` 拼接。

### 2.3 开发环境代理 —— 新增路由需同步注册

`vite.config.ts` 在配置了 `VITE_API_PREFIX` 时，会为带前缀的路径注册一组代理规则（剥掉前缀后转发到对应后端）。若新增了后端路由（如 `/xxx-api`）：

1. 在 `server.proxy` 中注册**原始路径规则**（如 `'/xxx-api'`）；
2. 同时注册**带前缀规则**（如 `` `${apiPrefix}/xxx-api` ``，`rewrite` 剥掉前缀转发）；
3. 否则开发环境下带前缀的请求会 404。

代理失败（后端未启动 / 连接被拒）时返回结构化 502 JSON（`code: BACKEND_SERVICE_UNREACHABLE`），可直接据此定位。

### 2.4 生产环境网关 —— 转发规则

生产部署时，网关 / 门户需将 `${VITE_API_PREFIX}` 前缀下的请求反向代理到对应后端服务。当接口请求收到 HTML 响应（如门户登录页、反代错误页）时，前端会返回结构化错误 `PROXY_ROUTING_ERROR`，提示代理未命中后端服务。

## 3. 相关工具函数（`src/utils/env.ts`）

| 函数 | 说明 |
|---|---|
| `getApiPrefix()` | 返回规范化后的接口前缀（如 `/jiuwenswarm/clawmanagerweb/chat/api`），未配置返回空串 |
| `resolveApiUrl(path)` | 相对路径加前缀；绝对地址（`http(s)://`、`//`、`data:`、`blob:`）原样返回；幂等 |
| `getWsBase()` | 未显式配置 `VITE_WS_BASE` 时，基于页面 origin + 前缀推导 WS 地址 |
| `getGatewayHttpBase()` | 默认 `/gateway-api/v1`；配置前缀时自动带上前缀 |

## 4. 常见问题

- **会重复加前缀吗？** 不会。`resolveApiUrl` 与 fetch 拦截器均做了幂等处理，不会出现 `/api/api/`；
- **未配置前缀时怎么办？** 无需任何处理，全部逻辑走原路径，行为与改动前一致；
- **如何判断代理是否命中？** 收到 HTML 响应 → `PROXY_ROUTING_ERROR`；收到 502 JSON → `BACKEND_SERVICE_UNREACHABLE`，按错误信息检查后端服务与网关转发配置。
