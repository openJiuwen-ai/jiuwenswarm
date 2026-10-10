# API 设计与治理

## 核心原则

API 是系统间的契约。好的 API 设计降低集成成本，好的 API 治理保证契约长期稳定。

## API 风格选型

### 决策树

```
谁消费 API？
├── 同一团队，前后端都是 TypeScript
│   └── tRPC（端到端类型安全，零代码生成）
├── 同一组织，多语言后端
│   └── REST + OpenAPI（通用性强，工具链成熟）
├── 外部第三方消费
│   ├── 查询灵活度高 → GraphQL（客户端按需查询）
│   └── 标准化操作 → REST + OpenAPI SDK 生成
├── 内部服务间高性能通信
│   └── gRPC（Protobuf 二进制，双向流）
└── 事件通知/Webhook
    └── 事件驱动 + CloudEvents 规范
```

### 风格对比

| 维度 | REST | GraphQL | gRPC | tRPC |
|------|------|---------|------|------|
| 消费者 | 通用 | 前端灵活查询 | 内部高性能 | 同语言全栈 |
| 类型安全 | OpenAPI 生成 | Schema 强类型 | Protobuf 强类型 | 自动推导 |
| 性能 | 中（JSON） | 中（JSON） | 高（二进制） | 中（JSON） |
| 浏览器友好 | 是 | 是 | 否（需 gRPC-Web） | 是 |
| 学习成本 | 低 | 中 | 中 | 低 |
| 缓存 | HTTP 缓存成熟 | 需自建缓存 | 无 | 无 |
| 版本化 | URL/Header | Schema 演进 | Protobuf 兼容 | 代码层 |

## REST API 设计规范

### URL 设计

```
✅ 资源用名词复数：/api/v1/orders
✅ 嵌套表达从属：/api/v1/users/{userId}/orders
✅ 查询参数过滤：/api/v1/orders?status=pending&page=2&limit=20
✅ 动作用子资源：/api/v1/orders/{id}/cancel

❌ 动词在 URL：/api/v1/getOrders
❌ 文件扩展名：/api/v1/orders.json
❌ 超过两层嵌套：/api/v1/users/{id}/orders/{id}/items/{id}/reviews
```

### HTTP 状态码使用

| 状态码 | 语义 | 使用场景 |
|--------|------|---------|
| 200 OK | 成功 | GET/PUT/PATCH 成功 |
| 201 Created | 创建成功 | POST 成功 |
| 204 No Content | 成功无返回体 | DELETE 成功 |
| 400 Bad Request | 客户端错误 | 参数校验失败 |
| 401 Unauthorized | 未认证 | 缺少/无效 Token |
| 403 Forbidden | 无权限 | 认证通过但无授权 |
| 404 Not Found | 资源不存在 | 资源 ID 无效 |
| 409 Conflict | 冲突 | 并发冲突/重复创建 |
| 422 Unprocessable | 语义错误 | 业务规则校验失败 |
| 429 Too Many | 限流 | 触发速率限制 |
| 500 | 服务端错误 | 未捕获异常 |
| 503 | 服务不可用 | 健康检查失败/维护中 |

### 分页

```
# 偏移分页（简单，大数据量性能差）
GET /api/v1/orders?page=3&limit=20

# 游标分页（大数据量性能好，不支持随机跳页）
GET /api/v1/orders?cursor=eyJpZCI6MTAwfQ&limit=20
Response: { data: [...], next_cursor: "eyJpZCI6MTIwfQ" }
```

### 错误响应格式

```json
{
  "title": "Validation Error",
  "status": 422,
  "detail": "The given data was invalid",
  "errors": [
    { "field": "email", "message": "Email format is invalid" },
    { "field": "quantity", "message": "Quantity must be greater than 0" }
  ],
  "request_id": "req_abc123"
}
```

## API 版本化与向后兼容

### 版本化策略

| 策略 | 方式 | 适用场景 |
|------|------|---------|
| URL 版本 | /api/v1/orders, /api/v2/orders | 公开 API，消费者多 |
| Header 版本 | Accept: application/vnd.api.v2+json | 不想污染 URL |
| 无版本 | 向后兼容变更 | 内部 API，同团队 |

### 向后兼容规则

```
✅ 兼容变更（不升版本）：
- 新增可选字段（请求和响应）
- 新增端点
- 新增可选查询参数
- 放宽校验规则（如 minLength 5 → 3）

❌ 破坏性变更（必须升版本）：
- 删除或重命名字段
- 改变字段类型
- 收紧校验规则
- 改变默认行为
- 新增必填字段
```

### 废弃流程

```
1. 标记废弃（响应头：Deprecation: true, Sunset: 2026-12-31）
2. 通知消费者（文档 + 邮件 + API 网关告警）
3. 过渡期（至少 6 个月，双版本并行）
4. 监控旧版本调用量（降到 0 后下线）
5. 下线旧版本
```

## API 网关模式

### 网关职责

```
请求 → ┌─ 认证 ─┐
       ├─ 限流 ─┤
       ├─ 路由 ─┤ → 后端服务
       ├─ 聚合 ─┤（BFF：多服务响应合并）
       ├─ 缓存 ─┤
       └─ 日志 ─┘
```

### BFF（Backend for Frontend）模式

```
Web 客户端 → Web BFF →├→ 订单服务
                       ├→ 用户服务
                       └→ 库存服务

Mobile 客户端 → Mobile BFF →├→ 订单服务（精简字段）
                            ├→ 用户服务（精简字段）
                            └→ 库存服务
```

**适用场景：**
- 不同客户端需要不同数据粒度
- 前端需要聚合多个后端服务
- 减少前端到后端的请求往返

### 网关选型

| 方案 | 适用场景 | 关键能力 |
|------|---------|---------|
| Nginx/Ingress | 简单路由 + TLS | 反向代理、负载均衡 |
| Kong | API 治理 | 插件生态、认证、限流 |
| APISIX | 动态配置 | 热更新、插件链 |
| 自研 BFF | 复杂聚合 | 按需聚合、客户端定制 |

## 契约管理与 OpenAPI

### OpenAPI 规范

```yaml
openapi: 3.1.0
info:
  title: Order Service API
  version: 1.0.0
paths:
  /api/v1/orders:
    post:
      summary: Create order
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateOrderRequest'
      responses:
        '201':
          description: Order created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Order'
        '422':
          $ref: '#/components/responses/ValidationError'
components:
  schemas:
    CreateOrderRequest:
      type: object
      required: [items]
      properties:
        items:
          type: array
          items:
            $ref: '#/components/schemas/OrderItem'
```

### 契约测试

```
✅ OpenAPI Spec 是唯一事实来源
✅ CI 中校验实现与 Spec 一致（契约测试）
✅ 从 Spec 生成客户端 SDK（多语言）
✅ 从 Spec 生成 Mock Server（前端并行开发）
✅ Spec 入版本控制，PR Review 变更

❌ 先写代码再补 Spec
❌ Spec 与实现不一致
❌ 手写客户端 SDK
```
