# 认证与授权架构

## 认证方式选型

| 方式 | 适用场景 | 优势 | 劣势 |
|------|---------|------|------|
| JWT | 无状态 API、微服务 | 自包含、无服务端存储 | 无法主动失效、Token 大 |
| Session | 传统 Web、SSR | 可主动失效、简单 | 需共享 Session 存储 |
| OAuth2/OIDC | 第三方登录、SSO | 标准化、委托授权 | 实现复杂 |
| API Key | 服务间调用、BFF | 简单 | 无用户上下文 |

### JWT vs Session 决策

```
需要无状态（不共享 Session 存储）？
├── 是 → JWT
│   ├── 需要主动失效 → JWT + Redis 黑名单（短 TTL）
│   └── 不需要主动失效 → 纯 JWT（短 TTL + Refresh Token）
└── 否 → Session
    ├── 单体应用 → 内存/Redis Session
    └── 多实例 → Redis 共享 Session
```

## JWT Bearer 流程

### Token 结构

```
Access Token（短 TTL: 15 分钟）
  Payload: { sub: userId, roles: [...], iat, exp }

Refresh Token（长 TTL: 7 天，服务端存储）
  Payload: { sub: userId, jti: uniqueId, iat, exp }
```

### 流程

```
1. 登录：POST /api/auth/login { email, password }
   → 返回 { accessToken, refreshToken(httpOnly cookie) }

2. 访问：GET /api/orders
   Header: Authorization: Bearer <accessToken>

3. 刷新：POST /api/auth/refresh
   Cookie: refreshToken=<httpOnly>
   → 返回 { accessToken }（刷新 Refresh Token）

4. 登出：POST /api/logout
   → 清除 Refresh Token + Cookie
```

### JWT 规则

```
✅ 短 TTL Access Token（15 分钟）+ 服务端存储 Refresh Token
✅ 最小 Claims：userId, roles（不放整个用户对象）
✅ 定期轮换签名密钥
✅ Refresh Token 存 httpOnly Cookie（防 XSS）

❌ 不存 Token 在 localStorage（XSS 风险）
❌ 不在 URL 参数中传 Token
❌ 不放敏感信息在 JWT Payload（Base64 可解码）
```

## Token 自动刷新

```typescript
async function apiWithRefresh<T>(path: string, options: RequestInit = {}): Promise<T> {
  try {
    return await api<T>(path, options);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      const refreshed = await api<{ accessToken: string }>('/api/auth/refresh', {
        method: 'POST',
        credentials: 'include',  // 发送 httpOnly cookie
      });
      setAuthToken(refreshed.accessToken);
      return api<T>(path, options);  // 重试原请求
    }
    throw err;
  }
}
```

## RBAC（基于角色的访问控制）

### 模式

```typescript
function authorize(...roles: Role[]) {
  return (req, res, next) => {
    if (!req.user) throw new UnauthorizedError();
    if (!roles.some(r => req.user.roles.includes(r))) throw new ForbiddenError();
    next();
  };
}

// 使用
router.delete('/users/:id', authenticate, authorize('admin'), deleteUser);
router.get('/orders/:id', authenticate, authorize('user', 'admin'), getOrder);
```

### 权限检查层次

```
1. 认证（Authentication）— 你是谁？
   → JWT 验证 / Session 检查

2. 授权（Authorization）— 你能做什么？
   → RBAC 角色检查

3. 资源所有权（Ownership）— 你能操作这个资源吗？
   → 水平越权检查：req.user.id === resource.ownerId

4. 业务规则（Business Rule）— 此操作在当前状态下允许吗？
   → 如：订单已发货不能取消
```

```
✅ 每个端点都检查认证 + 授权
✅ 资源操作检查所有权（防水平越权）
✅ 管理端点检查角色（防垂直越权）
✅ 权限检查在中间件 + Service 双层

❌ 不信任客户端传的 userId（从 Token 取，不从 Body 取）
❌ 不只检查角色不检查所有权
```

## 中间件顺序

```
请求 → 1.RequestID → 2.Logging → 3.CORS → 4.RateLimit → 5.BodyParse
     → 6.Auth → 7.Authz → 8.Validation → 9.Handler → 10.ErrorHandler → 响应
```

| 顺序 | 中间件 | 职责 |
|------|--------|------|
| 1 | RequestID | 生成请求 ID，贯穿日志链路 |
| 2 | Logging | 记录请求入口，带 RequestID |
| 3 | CORS | 处理跨域预检 |
| 4 | RateLimit | 限流（认证前限流防暴力破解） |
| 5 | BodyParse | 解析请求体 |
| 6 | Auth | 验证 Token，注入 req.user |
| 7 | Authz | 角色和权限检查 |
| 8 | Validation | 输入校验（Zod/Pydantic） |
| 9 | Handler | 业务处理 |
| 10 | ErrorHandler | 全局错误捕获 |

## OAuth2/OIDC 集成

### 授权码流程（最常用）

```
1. 客户端重定向到 IdP 授权端点
   GET https://idp.com/auth?response_type=code&client_id=xxx&redirect_uri=xxx&scope=openid+profile

2. 用户在 IdP 登录并授权

3. IdP 重定向回客户端，带 code
   GET https://app.com/callback?code=xxx

4. 客户端用 code 换 Token
   POST https://idp.com/token
   Body: grant_type=authorization_code&code=xxx&client_id=xxx&client_secret=xxx

5. 客户端用 Access Token 获取用户信息
   GET https://idp.com/userinfo
   Header: Authorization: Bearer <accessToken>
```

### 适用场景

```
需要第三方登录（Google/GitHub/微信）？→ OAuth2 授权码流程
需要 SSO（多应用单点登录）？→ OIDC（OAuth2 + ID Token）
需要服务间调用？→ Client Credentials Grant
需要移动端登录？→ PKCE 扩展（无 client_secret）
```
