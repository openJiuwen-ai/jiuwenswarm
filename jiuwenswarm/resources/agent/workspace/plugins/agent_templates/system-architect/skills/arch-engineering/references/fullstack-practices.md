# 全栈开发架构实践

## 核心原则（7 条铁律）

```
1. ✅ 按 FEATURE 组织代码，不按技术层
2. ✅ Controller 不含业务逻辑
3. ✅ Service 不导入 HTTP 类型
4. ✅ 所有配置走环境变量，启动校验，快速失败
5. ✅ 每个错误都有类型，全局处理，统一格式
6. ✅ 所有输入在边界校验——不信任客户端
7. ✅ 结构化 JSON 日志 + 请求 ID——不用 console.log
```

## 项目结构与分层

### Feature-First 组织

```
✅ Feature-first                    ❌ Layer-first
src/                                src/
  orders/                             controllers/
    order.controller.ts                 order.controller.ts
    order.service.ts                   user.controller.ts
    order.repository.ts              services/
    order.dto.ts                        order.service.ts
    order.test.ts                       user.service.ts
  users/                              repositories/
    user.controller.ts                  ...
    user.service.ts
  shared/
    database/
    middleware/
```

### 三层架构

```
Controller (HTTP) → Service (业务逻辑) → Repository (数据访问)
```

| 层 | 职责 | ❌ 禁止 |
|------|------|------|
| Controller | 解析请求、校验、调用 Service、格式化响应 | 业务逻辑、DB 查询 |
| Service | 业务规则、编排、事务管理 | HTTP 类型（req/res）、直接 DB |
| Repository | 数据库查询、外部 API 调用 | 业务逻辑、HTTP 类型 |

### 依赖注入（多语言）

```typescript
// TypeScript
class OrderService {
  constructor(
    private readonly orderRepo: OrderRepository,
    private readonly emailService: EmailService,
  ) {}
}
```

```python
# Python
class OrderService:
    def __init__(self, order_repo: OrderRepository, email_service: EmailService):
        self.order_repo = order_repo
        self.email_service = email_service
```

```go
// Go
type OrderService struct {
    orderRepo    OrderRepository
    emailService EmailService
}
func NewOrderService(repo OrderRepository, email EmailService) *OrderService {
    return &OrderService{orderRepo: repo, emailService: email}
}
```

## 配置管理

配置层级、环境变量校验、.env.example 规范和 CORS 配置见：
`environment-management.md`

## 错误处理与韧性

### 类型化错误体系

```typescript
class AppError extends Error {
  constructor(message: string, public readonly code: string,
    public readonly statusCode: number, public readonly isOperational: boolean = true) {
    super(message);
  }
}
class NotFoundError extends AppError {
  constructor(resource: string, id: string) {
    super(`${resource} not found: ${id}`, 'NOT_FOUND', 404);
  }
}
class ValidationError extends AppError {
  constructor(public readonly errors: FieldError[]) {
    super('Validation failed', 'VALIDATION_ERROR', 422);
  }
}
```

### 全局错误处理

```typescript
app.use((err, req, res, next) => {
  if (err instanceof AppError && err.isOperational) {
    return res.status(err.statusCode).json({
      title: err.code, status: err.statusCode,
      detail: err.message, request_id: req.id,
    });
  }
  logger.error('Unexpected error', { error: err.message, stack: err.stack, request_id: req.id });
  res.status(500).json({ title: 'Internal Error', status: 500, request_id: req.id });
});
```

```
✅ 类型化、领域特定的错误类
✅ 全局错误处理器捕获一切
✅ 操作错误 → 结构化响应
✅ 编程错误 → 日志 + 通用 500
✅ 瞬时故障用指数退避重试

❌ 不静默捕获并忽略错误
❌ 不向客户端返回堆栈跟踪
❌ 不抛出通用 Error('something')
```

## 数据库访问模式

迁移管理（迁移规则、大表分步变更、扩展表模式）见：
`arch-evolution` 技能的 `db-schema.md` 参考

### N+1 查询预防

```typescript
// ❌ N+1: 1 次查询 + N 次查询
const orders = await db.order.findMany();
for (const o of orders) {
  o.items = await db.item.findMany({ where: { orderId: o.id } });
}

// ✅ 单次 JOIN 查询
const orders = await db.order.findMany({ include: { items: true } });
```

### 事务与连接池

```typescript
await db.$transaction(async (tx) => {
  const order = await tx.order.create({ data: orderData });
  await tx.inventory.decrement({ productId, quantity });
  await tx.payment.create({ orderId: order.id, amount });
});
```

连接池大小 = `(CPU 核数 × 2) + 磁盘数`（起始 10-20）。始终设置连接超时。Serverless 用 PgBouncer。

## 缓存模式

### Cache-Aside（懒加载）

```typescript
async function getUser(id: string): Promise<User> {
  const cached = await redis.get(`user:${id}`);
  if (cached) return JSON.parse(cached);

  const user = await userRepo.findById(id);
  if (!user) throw new NotFoundError('User', id);

  await redis.set(`user:${id}`, JSON.stringify(user), 'EX', 900);  // 15min TTL
  return user;
}
```

| 数据类型 | 建议 TTL |
|---------|---------|
| 用户资料 | 5-15 分钟 |
| 商品目录 | 1-5 分钟 |
| 配置/特性开关 | 30-60 秒 |
| 会话 | 匹配会话时长 |

```
✅ 始终设 TTL——不缓存不过期
✅ 写后失效（更新后删缓存键）
✅ 缓存用于读，不用于权威状态
❌ 不无 TTL 缓存（过期数据比慢数据更糟）
```

## 后台任务

```
✅ 所有任务必须幂等（同一任务跑两次 = 同一结果）
✅ 失败任务 → 重试（最多 3 次）→ 死信队列 → 告警
✅ Worker 作为独立进程运行（不是 API 服务器中的线程）

❌ 不在请求处理器中放长耗时任务
❌ 不假设任务恰好执行一次
```

## 实时通信模式

| 方式 | 方向 | 复杂度 | 适用场景 |
|------|------|--------|---------|
| 轮询 | 客户端 → 服务端 | 低 | 简单状态检查，< 10 客户端 |
| SSE | 服务端 → 客户端 | 中 | 通知、Feed、AI 流式响应 |
| WebSocket | 双向 | 高 | 聊天、协作编辑、游戏 |

## 生产加固

健康检查、优雅停机、安全检查清单见：
`arch-review` 技能的 `review-checklist.md`（高可用性 + 安全性清单）
`arch-review` 技能的 `security-architecture.md`（安全架构评审清单）

## 反模式速查表

| # | ❌ 不要 | ✅ 应该 |
|---|--------|--------|
| 1 | 业务逻辑在 Controller | 移到 Service 层 |
| 2 | process.env 散落各处 | 集中类型化配置 |
| 3 | console.log 日志 | 结构化 JSON 日志 |
| 4 | 通用 Error('oops') | 类型化错误体系 |
| 5 | Controller 直连 DB | Repository 模式 |
| 6 | 无输入校验 | 边界校验（Zod/Pydantic） |
| 7 | 静默捕获错误 | 日志 + 重抛或返回错误 |
| 8 | 无健康检查端点 | /health + /ready |
| 9 | 硬编码配置/密钥 | 环境变量 |
| 10 | 无优雅停机 | 正确处理 SIGTERM |
