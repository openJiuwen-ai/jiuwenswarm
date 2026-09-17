# 可观测性建设

## 核心原则

可观测性不是"加日志"，而是让系统自己能回答"出了什么问题、为什么、在哪里"。

```
三大支柱：
1. 日志（Logs）— 发生了什么（事后排查）
2. 指标（Metrics）— 系统状态如何（实时监控）
3. 链路追踪（Traces）— 请求经过了哪里（跨服务诊断）
```

## OpenTelemetry 集成

### 自动埋点

```typescript
// Node.js 自动埋点
import { NodeSDK } from '@opentelemetry/sdk-node';
import { getNodeAutoInstrumentations } from '@opentelemetry/auto-instrumentations-node';

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({ url: 'http://otel-collector:4318' }),
  instrumentations: [getNodeAutoInstrumentations()],
});
sdk.start();
// 自动埋点：HTTP、Express、gRPC、数据库驱动、Redis
```

```python
# Python 自动埋点
from opentelemetry.instrumentation.auto_instrumentation import site_packages
# 启动时注入：opentelemetry-instrument python app.py
# 自动埋点：Flask、Django、Requests、SQLAlchemy、Redis
```

### 手动埋点（业务关键路径）

```typescript
import { trace, SpanStatusCode } from '@opentelemetry/api';
const tracer = trace.getTracer('order-service');

app.post('/api/orders', async (req, res) => {
  const span = tracer.startSpan('create_order');
  try {
    span.setAttribute('order.user_id', req.user.id);
    span.setAttribute('order.item_count', req.body.items.length);
    const order = await orderService.create(req.body);
    span.setAttribute('order.id', order.id);
    span.setStatus({ code: SpanStatusCode.OK });
    res.json(order);
  } catch (err) {
    span.recordException(err);
    span.setStatus({ code: SpanStatusCode.ERROR, message: err.message });
    throw err;
  } finally {
    span.end();
  }
});
```

### Trace 串联

```
请求链路：
客户端 → API 网关 → 订单服务 → 支付服务 → 数据库
         ↓ TraceContext 传播 ↓

关键：W3C TraceContext Header（traceparent）自动传播
- 自动埋点：HTTP/gRPC 自动传播
- 手动传播：消息队列需显式注入和提取
```

```typescript
// 消息队列显式传播
import { trace, propagation } from '@opentelemetry/api';

// 生产者：注入 trace context
function publishEvent(event) {
  const carrier = {};
  propagation.inject(carrier);  // 注入到 carrier
  messageQueue.publish({ ...event, _trace: carrier });
}

// 消费者：提取 trace context
function consumeEvent(message) {
  const carrier = message._trace;
  const context = propagation.extract(carrier);
  const span = tracer.startSpan('process_event', { context });
  // 处理事件...
  span.end();
}
```

## 日志规范

### 结构化日志

```typescript
// ✅ 结构化 — 可解析、可过滤、可告警
logger.info('Order created', {
  orderId: order.id,
  userId: user.id,
  total: order.total,
  itemCount: order.items.length,
  duration_ms: Date.now() - startTime,
  trace_id: span.spanContext().traceId,  // 关联链路追踪
});

// 输出：{"level":"info","msg":"Order created","orderId":"ord_123","trace_id":"abc..."}
```

### 日志级别

| 级别 | 何时使用 | 生产环境 |
|------|---------|---------|
| error | 需要立即关注 | ✅ 始终 |
| warn | 意外但已处理 | ✅ 始终 |
| info | 正常操作，审计 | ✅ 始终 |
| debug | 开发排查 | ❌ 仅开发环境 |

### 日志规则

```
✅ 每条日志带 request_id / trace_id（关联链路追踪）
✅ 在层边界记录（请求进入、响应返回、外部调用）
✅ 错误日志含上下文（什么操作、什么参数、什么结果）

❌ 不记录密码、Token、PII、密钥
❌ 不用 console.log（无结构化、无级别、无关联）
❌ 不在循环内打 info 日志（日志风暴）
```

## 指标埋点

### RED 指标（服务层）

```
Rate     — 请求速率（QPS）
Errors   — 错误率
Duration — 延迟分布（P50/P95/P99）
```

```typescript
// Prometheus 指标定义
import { Counter, Histogram } from 'prom-client';

const httpRequestTotal = new Counter({
  name: 'http_requests_total',
  help: 'Total HTTP requests',
  labelNames: ['method', 'route', 'status'],
});

const httpRequestDuration = new Histogram({
  name: 'http_request_duration_seconds',
  help: 'HTTP request duration',
  labelNames: ['method', 'route'],
  buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5],  // 秒
});

// 中间件埋点
app.use((req, res, next) => {
  const start = Date.now();
  res.on('finish', () => {
    const duration = (Date.now() - start) / 1000;
    httpRequestTotal.inc({ method: req.method, route: req.route?.path, status: res.statusCode });
    httpRequestDuration.observe({ method: req.method, route: req.route?.path }, duration);
  });
  next();
});
```

### USE 指标（资源层）

```
Utilization  — 利用率（CPU/内存/磁盘/网络）
Saturation   — 饱和度（队列长度/连接池使用率）
Errors       — 错误（磁盘错误/网络丢包）
```

### 业务指标

```typescript
// 业务关键指标（不只是技术指标）
const ordersCreated = new Counter({ name: 'orders_created_total', labelNames: ['channel'] });
const orderValue = new Histogram({ name: 'order_value_cents', buckets: [1000, 5000, 10000, 50000] });
const paymentFailed = new Counter({ name: 'payment_failed_total', labelNames: ['reason'] });
```

## 告警规则

### 告警分级

| 级别 | 触发条件 | 通知方式 | 响应时限 |
|------|---------|---------|---------|
| P0-致命 | 可用性 < 99% / 核心功能不可用 | 电话 + 短信 | 5 分钟 |
| P1-高 | 错误率 > 1% / P99 > SLO | 短信 + IM | 15 分钟 |
| P2-中 | 资源利用率 > 85% / 延迟上升 | IM | 1 小时 |
| P3-低 | 非核心指标异常 | 邮件 | 工作日 |

### 告警规则设计

```
✅ 基于用户影响告警（错误率/延迟），不只基于资源
✅ 每条告警有明确的处理流程（runbook）
✅ 告警有冷却期（避免风暴）
✅ 分级路由（P0→电话，P3→邮件）

❌ 不告警 CPU > 50%（无上下文，可能正常）
❌ 不告警单次错误（可能是偶发）
❌ 不所有告警都发电话（告警疲劳）
```

## 仪表盘

### 核心仪表盘

| 仪表盘 | 面板内容 | 受众 |
|--------|---------|------|
| 服务健康 | RED 指标 + 错误率 + SLO | on-call |
| 业务概览 | 订单量/支付量/用户活跃 | 产品/业务 |
| 基础设施 | CPU/内存/磁盘/网络 | 运维 |
| 链路追踪 | 请求拓扑 + 慢请求分析 | 开发 |
