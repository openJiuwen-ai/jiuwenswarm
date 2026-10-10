# 数据库 Schema 设计

## 设计原则

```
1. 先建模领域，再设计 Schema——Schema 服务于领域模型
2. 规范化优先，反范式有据——每次反范式都要说明理由
3. 索引按查询建，不按字段建——每个索引都有对应查询场景
4. 迁移可回滚——每个 UP 迁移都有对应 DOWN 迁移
5. 不存可计算的字段——除非有明确性能瓶颈且缓存无效
```

## 索引策略

### 索引类型选择

| 索引类型 | 适用场景 | 示例 |
|---------|---------|------|
| B-Tree | 等值查询、范围查询、排序 | WHERE id = ? / WHERE created_at > ? |
| Hash | 仅等值查询 | WHERE status = 'active' |
| GIN | 多值列（数组、JSONB、全文） | WHERE tags @> ['sale'] |
| GiST | 地理空间、范围类型 | WHERE location && box |
| Partial | 只索引满足条件的行 | WHERE deleted_at IS NULL |

### 索引设计规则

```
✅ 为高频查询的 WHERE / JOIN / ORDER BY 列建索引
✅ 复合索引遵循最左前缀原则
✅ 覆盖索引避免回表（索引包含查询所需所有列）
✅ 部分索引减少索引大小（WHERE deleted_at IS NULL）

❌ 不为低基数列单独建索引（如 gender）
❌ 不为从不查询的列建索引
❌ 不建过多索引（写入性能下降 + 存储成本）
❌ 不忽略 EXPLAIN ANALYZE 验证
```

### 复合索引顺序

```
原则：等值条件在前，范围条件在后

✅ CREATE INDEX idx_orders ON orders(user_id, created_at)
   查询：WHERE user_id = ? AND created_at > ?  → 命中

❌ CREATE INDEX idx_orders ON orders(created_at, user_id)
   查询：WHERE user_id = ? AND created_at > ?  → 部分命中（先按 created_at 范围扫描）
```

## 迁移管理

### 迁移规则

```
✅ 所有 Schema 变更走迁移工具（Alembic / Prisma Migrate / golang-migrate）
✅ 每个迁移必须可回滚（UP + DOWN）
✅ 生产前在 Staging 验证迁移
✅ 大表迁移分步执行（见下方扩展模式）
✅ 迁移文件入版本控制

❌ 不手动执行 SQL 修改 Schema
❌ 不在生产直接 ALTER 大表
❌ 不删除迁移文件（即使回滚）
```

### 大表 Schema 变更（扩展模式）

大表（> 1000 万行）的 Schema 变更必须分步：

```
阶段 1：添加可空列（ALTER TABLE ADD COLUMN，快速）
阶段 2：应用层双写（旧字段 + 新字段同时写）
阶段 3：回填历史数据（分批 UPDATE，避免锁表）
阶段 4：应用层切读到新字段
阶段 5：移除旧字段（确认无引用后）
```

### 扩展表模式

当需要给现有表加大量字段时，用扩展表而非不断加列：

```
-- ❌ 不断加列
ALTER TABLE users ADD COLUMN preferences JSONB;
ALTER TABLE users ADD COLUMN settings JSONB;
ALTER TABLE users ADD COLUMN metadata JSONB;

-- ✅ 扩展表
CREATE TABLE user_extensions (
  user_id UUID REFERENCES users(id),
  extension_type VARCHAR(50),  -- preferences / settings / metadata
  extension_data JSONB,
  PRIMARY KEY (user_id, extension_type)
);
```

## 多租户模式

| 模式 | 隔离级别 | 复杂度 | 适用场景 |
|------|---------|--------|---------|
| 共享 Schema + tenant_id | 低 | 低 | 小规模，成本敏感 |
| Schema-per-tenant | 中 | 中 | 中等规模，需要隔离 |
| Database-per-tenant | 高 | 高 | 大客户，强隔离，合规要求 |

### 共享 Schema 模式

```sql
CREATE TABLE orders (
  id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,  -- 每个查询必须带 tenant_id
  user_id UUID NOT NULL,
  total DECIMAL(10,2),
  created_at TIMESTAMPTZ,
  -- 每个索引必须带 tenant_id 前缀
  INDEX idx_tenant_user (tenant_id, user_id),
  INDEX idx_tenant_created (tenant_id, created_at)
);

-- 行级安全（PostgreSQL RLS）
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON orders
  USING (tenant_id = current_setting('app.tenant_id')::UUID);
```

### 决策规则

```
租户数量？
├── < 50 且需要强隔离 → Database-per-tenant
├── 50-5000 且成本敏感 → Schema-per-tenant
└── > 5000 或 SaaS → 共享 Schema + tenant_id + RLS
```

## 分片策略

| 策略 | 方式 | 适用场景 | 挑战 |
|------|------|---------|------|
| 范围分片 | 按值范围（日期/ID 段） | 时间序列数据 | 热点（最新分片） |
| 哈希分片 | hash(key) % N | 均匀分布 | 扩容需 reshard |
| 地理分片 | 按地理区域 | 低延迟、合规 | 跨区域查询 |
| 一致性哈希 | 环形哈希空间 | 动态扩容 | 实现复杂 |

### 分片决策

```
单库能否满足？
├── 是 → 不分片（默认）
└── 否 → 瓶颈类型？
    ├── 写入瓶颈 → 读写分离 + 写分片
    ├── 数据量瓶颈 → 按时间/ID 分片
    └── 地理延迟 → 地理分片
```

## 常见反模式

| 反模式 | 问题 | 修复 |
|--------|------|------|
| EAV（实体-属性-值） | 查询复杂、无类型安全 | 用 JSONB 或扩展表 |
| 软删除无索引 | 查询全表扫描 | 部分索引 WHERE deleted_at IS NULL |
| 外键缺失 | 数据完整性无保障 | 加外键约束 |
| 无主键 | 复制/分片困难 | 每表必有主键（UUID 或自增） |
| 存计算字段 | 数据不一致 | 用生成列或视图 |
| 大 JSON 列 | 查询性能差 | 关键字段提取为列，其余留 JSONB |
