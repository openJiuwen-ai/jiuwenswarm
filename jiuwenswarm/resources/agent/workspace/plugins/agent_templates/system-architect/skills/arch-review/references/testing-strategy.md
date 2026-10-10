# 测试策略

## 测试金字塔

```
        /\
       /E2E\        少量 — 关键用户路径，慢，脆弱
      /------\
     /Integration\   适量 — 真实依赖，中速
    /--------------\
   /     Unit      \   大量 — 快，隔离，覆盖率高
  /------------------\
```

| 层级 | 占比 | 速度 | 范围 | 依赖 |
|------|------|------|------|------|
| 单元测试 | 70% | 毫秒级 | 单个函数/类 | Mock 依赖 |
| 集成测试 | 20% | 秒级 | 模块间交互 | 测试容器/事务回滚 |
| E2E 测试 | 10% | 分钟级 | 完整用户流程 | 完整环境 |

## 单元测试

### 原则

```
✅ 测试行为，不测实现——不断言私有方法
✅ 一个测试只验证一个行为
✅ Arrange-Act-Assert（AAA）模式
✅ Mock Repository 层（快），不 Mock Service 层
✅ 测试名描述意图：should_create_order_when_items_valid
```

### 示例

```typescript
describe('OrderService', () => {
  let service: OrderService;
  let mockRepo: jest.Mocked<OrderRepository>;

  beforeEach(() => {
    mockRepo = { create: jest.fn(), findById: jest.fn() };
    service = new OrderService(mockRepo);
  });

  it('should create order when items are valid', async () => {
    // Arrange
    const input = { items: [{ productId: 'p1', quantity: 2 }] };
    mockRepo.create.mockResolvedValue({ id: 'ord_1', total: 100 });

    // Act
    const result = await service.create(input);

    // Assert
    expect(result.id).toBe('ord_1');
    expect(mockRepo.create).toHaveBeenCalledWith(input);
  });

  it('should throw ValidationError when items empty', async () => {
    await expect(service.create({ items: [] })).rejects.toThrow(ValidationError);
  });
});
```

## 集成测试

### 策略

| 方式 | 优点 | 缺点 | 适用 |
|------|------|------|------|
| 测试容器 | 真实 DB，隔离 | 启动慢 | 需要真实 DB 行为 |
| 事务回滚 | 快，真实 DB | 不能测跨连接事务 | 大多数集成测试 |
| 内存 DB | 最快 | SQL 方言差异 | 简单 CRUD |

### 示例（事务回滚）

```typescript
describe('OrderRepository (integration)', () => {
  let db: Database;

  beforeEach(async () => {
    db = await createTestDatabase();
    await db.migrate();
  });

  afterEach(async () => {
    await db.close();
  });

  it('should persist order with items', async () => {
    await db.transaction(async (tx) => {
      const repo = new OrderRepository(tx);
      await repo.create({ items: [{ productId: 'p1', quantity: 2 }] });

      const found = await repo.findAll();
      expect(found).toHaveLength(1);
      expect(found[0].items).toHaveLength(1);

      throw new Error('rollback');  // 回滚事务
    }).catch(() => {});  // 忽略回滚错误
  });
});
```

## 契约测试

### 消费者驱动契约（CDC）

```
消费者（前端）定义期望 → 提供者（后端）验证满足

工具：Pact
流程：
1. 消费者编写交互期望（请求 + 期望响应）
2. 消费者测试生成 Pact 文件
3. 提供者回放 Pact 文件验证
4. CI 中提供者验证失败 → 阻止破坏性变更
```

```
✅ 契约测试替代重量级 E2E——更快更稳定
✅ 消费者定义需求，提供者保证满足
✅ 破坏性变更在 CI 阶段被拦截
✅ 微服务间接口稳定性保障

❌ 不用 E2E 测试覆盖所有服务间交互——太慢太脆弱
```

## 性能测试

| 类型 | 目的 | 工具 | 负载模式 |
|------|------|------|---------|
| 基准测试 | 单接口基线 | wrk/vegeta | 固定 QPS |
| 负载测试 | 找拐点 | k6/Locust | 阶梯递增 |
| 压力测试 | 找崩溃点 | k6/Locust | 持续加压 |
| 浸泡测试 | 内存泄漏 | k6 | 固定 24h+ |

## 测试策略决策

```
需要测试什么？
├── 单个函数/类的行为 → 单元测试（Mock 依赖）
├── 模块间数据流正确性 → 集成测试（真实 DB 或事务回滚）
├── API 接口契约稳定性 → 契约测试（Pact）
├── 完整用户流程 → E2E 测试（仅关键路径）
└── 性能/容量 → 性能测试（压测）
```

## 常见问题

### "业务规则放哪里？"

涉及 HTTP（请求解析、状态码、Header）→ Controller。涉及业务决策（定价、权限、规则）→ Service。涉及数据库 → Repository。

### "Service 太大了"

症状：一个 Service 文件 > 500 行，20+ 方法。
修复：按子域拆分。`OrderService` → `OrderCreationService` + `OrderFulfillmentService` + `OrderQueryService`。

### "测试慢因为打数据库"

修复：单元测试 Mock Repository（快）。集成测试用测试容器或事务回滚（真实 DB，仍然快）。集成测试中不 Mock Service 层。
