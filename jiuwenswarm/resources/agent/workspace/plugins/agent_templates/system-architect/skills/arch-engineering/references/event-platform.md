# 事件/消息平台实现参考

## 核心原则

事件驱动架构的核心不是"用消息队列"，而是"用事件解耦"。选对中间件、设计好事件 schema、保证消费者幂等，三件事做对，事件驱动才能落地。

## 消息中间件选型

| 中间件 | 吞吐量 | 延迟 | 有序性 | 持久化 | 适用场景 |
|--------|--------|------|--------|--------|---------|
| Kafka | 极高（百万/s） | 低 | 分区内有序 | 持久化 + 回放 | 高吞吐事件流、日志聚合、CDC |
| RabbitMQ | 中高（万/s） | 极低 | 队列内 FIFO | 可选持久化 | 任务队列、RPC、路由复杂 |
| NATS | 高 | 极低 | 无 | 可选（JetStream） | 轻量级、低延迟、IoT |
| Pulsar | 高 | 低 | 分区有序 | 持久化 + 分层存储 | 多租户、Geo 复制、流批一体 |
| Redis Streams | 中 | 极低 | 无 | 可选 | 已有 Redis、简单场景、短生命周期 |

### 选型决策树

```
吞吐量需求？
├── > 10 万/s → Kafka 或 Pulsar
│   ├── 需要多租户/Geo 复制 → Pulsar
│   └── 简单高吞吐 → Kafka
├── 1-10 万/s → RabbitMQ 或 NATS
│   ├── 需要复杂路由（topic/fanout/header） → RabbitMQ
│   └── 需要极低延迟 → NATS
└── < 1 万/s → Redis Streams
    └── 已有 Redis，不想引入新组件 → Redis Streams
```

## 事件命名规范

```
命名格式：{Domain}.{Aggregate}.{Action}  或  {Aggregate}{Action}

示例：
✅ OrderCreated       — 订单已创建
✅ PaymentCompleted   — 支付已完成
✅ InventoryReserved  — 库存已预留
✅ UserRegistered     — 用户已注册

规则：
✅ 过去时——事件是"已发生的事实"
✅ 领域前缀——标注来源限界上下文
✅ PascalCase——统一大小写风格
✅ 业务语义——不叫 DataSyncEvent 或 NotifyEvent

❌ OrderCreate（现在时，不是事实）
❌ OrderEvent（无具体动作）
❌ data_sync（技术名非业务名）
```

## 事件 Schema 设计

### 事件结构

```json
{
  "eventId": "evt_01HXYZ...",
  "eventType": "OrderCreated",
  "eventVersion": "1",
  "occurredAt": "2026-08-14T12:00:00Z",
  "aggregateId": "ord_12345",
  "aggregateType": "Order",
  "payload": {
    "orderId": "ord_12345",
    "userId": "usr_67890",
    "items": [
      { "productId": "prod_001", "quantity": 2, "price": 99.00 }
    ],
    "total": 198.00,
    "currency": "CNY"
  },
  "metadata": {
    "traceId": "trace_abc123",
    "correlationId": "corr_xyz789",
    "source": "order-service",
    "sourceVersion": "1.2.3"
  }
}
```

### Schema 演进规则

```
✅ 兼容变更（不升版本）：
- 新增可选字段（payload 中加新 key，消费者忽略未知字段）
- 新增事件类型（新 EventType，不影响已有消费者）

❌ 破坏性变更（必须升版本）：
- 删除或重命名字段 → eventVersion +1
- 改变字段类型 → eventVersion +1
- 改变语义（如 total 从含税改为不含税）→ eventVersion +1

版本兼容策略：
- 消费者按 eventVersion 路由处理逻辑
- 旧版本事件保留 N 天（过渡期），之后只发新版本
- Schema Registry（如 Confluent Schema Registry）强制校验
```

## 生产者模式

### Transactional Outbox（事务性发件箱）

问题：数据库提交和消息发送是两个操作，可能不一致。

```
❌ 错误做法：
1. 写数据库
2. 发消息
→ 如果步骤 2 失败，数据库有数据但消息没发

✅ 正确做法（Transactional Outbox）：
1. 在同一事务中：写业务表 + 写 outbox 表
2. 单独进程读 outbox 表，发消息，标记已发
3. 消息发送成功后删除/标记 outbox 记录
```

```sql
-- Outbox 表
CREATE TABLE event_outbox (
  id UUID PRIMARY KEY,
  event_type VARCHAR(100) NOT NULL,
  aggregate_id VARCHAR(100) NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  published_at TIMESTAMPTZ NULL,
  status VARCHAR(20) DEFAULT 'pending'  -- pending / published / failed
);

-- 同一事务中写入
BEGIN;
  INSERT INTO orders (...) VALUES (...);
  INSERT INTO event_outbox (event_type, aggregate_id, payload)
    VALUES ('OrderCreated', 'ord_123', '{"orderId":"ord_123",...}');
COMMIT;

-- 单独进程轮询发送
SELECT * FROM event_outbox WHERE status = 'pending' ORDER BY created_at LIMIT 100;
-- 发送到消息队列
-- 成功后：UPDATE event_outbox SET status = 'published', published_at = NOW() WHERE id = ...;
```

### CDC（变更数据捕获）

```
替代 Outbox 轮询的方案：CDC 直接捕获数据库变更

工具：Debezium
原理：读取数据库 WAL（Write-Ahead Log），将行变更转为事件

优势：
- 无需 Outbox 表，业务代码无感知
- 近实时（毫秒级延迟）
- 保留操作类型（INSERT/UPDATE/DELETE）

劣势：
- 基础设施复杂（Debezium + Kafka Connect）
- 全局有序性难保证
- Schema 变更需同步
```

## 消费者模式

### 幂等消费

```typescript
// 幂等消费者：同一事件处理多次结果一致
async function handleOrderCreated(event: OrderCreatedEvent) {
  // 1. 检查是否已处理
  const processed = await db.query(
    'SELECT 1 FROM processed_events WHERE event_id = $1', [event.eventId]
  );
  if (processed) {
    return;  // 已处理，跳过
  }

  // 2. 处理业务逻辑
  await inventoryService.reserve(event.payload.items);

  // 3. 标记已处理（同一事务）
  await db.query(
    'INSERT INTO processed_events (event_id, processed_at) VALUES ($1, NOW())',
    [event.eventId]
  );
}
```

### 消费者可靠性模式

| 模式 | 问题 | 解决方案 |
|------|------|---------|
| 幂等表 | 重复消费 | processed_events 表去重 |
| 死信队列 | 消费失败 | 失败 N 次后转入 DLQ，人工处理 |
| 重试+退避 | 瞬时故障 | 指数退避重试（1s→2s→4s→8s→DLQ） |
| 顺序消费 | 事件有序性需求 | 单分区 + 单消费者 |
| 批量消费 | 高吞吐 | 批量拉取 + 批量处理 |
| 优雅停机 | 消费中重启 | SIGTERM → 完成当前批次 → 提交 offset → 退出 |

### At-Least-Once vs Exactly-Once

```
At-Least-Once（至少一次）：
- 消息可能重复投递
- 消费者必须幂等
- 实现简单，大多数场景够用

Exactly-Once（恰好一次）：
- 消息不重复投递
- 需要事务性生产者 + 事务性消费者
- Kafka 事务 API 支持，但复杂
- 通常用 At-Least-Once + 幂等消费替代
```

## 事件溯源（Event Sourcing）

### 基本模式

```
传统 CRUD：只存当前状态
事件溯源：存所有事件，当前状态是事件回放的结果

Event Store:
- OrderCreated { items: [...], total: 198 }
- PaymentCompleted { paymentId: "pay_123", amount: 198 }
- OrderShipped { trackingNo: "SF123456" }

当前状态 = 回放所有事件 → Order { status: "shipped", paid: true, ... }
```

### 适用场景

```
✅ 适合事件溯源：
- 需要完整审计轨迹（金融、医疗）
- 需要时间旅行查询（"上周三这个订单是什么状态"）
- 领域事件本身就是核心业务资产
- 需要重建状态（CQRS 读模型重建）

❌ 不适合事件溯源：
- 简单 CRUD 领域
- 团队不熟悉
- 不需要审计轨迹
- 状态变更频率极高（事件存储膨胀）
```

### Snapshot（快照）优化

```
问题：回放 10000 个事件获取当前状态太慢

解决：定期快照
- 每 100 个事件存一次快照
- 回放时：加载最近快照 + 回放快照后的事件
- 快照 = 当前状态的序列化
```

## 事件管道监控

| 监控维度 | 指标 | 告警阈值 |
|---------|------|---------|
| 生产延迟 | 事件创建到发送的延迟 | > 5s |
| 消费延迟 | 事件到达到处理完成的延迟 | > 30s |
| 消费积压 | 队列中未消费消息数 | > 10000 |
| 死信队列 | DLQ 中消息数 | > 0 |
| 消费错误率 | 消费失败次数 / 总次数 | > 1% |
| 事件乱序 | 分区内 offset 回退 | > 0 |

```
✅ 监控事件端到端延迟（创建→消费），不只监控队列长度
✅ DLQ 有告警，消息进 DLQ 立即通知
✅ 消费者有独立的健康检查（不只是 HTTP /health）
✅ 事件 schema 变更有兼容性检查
```
