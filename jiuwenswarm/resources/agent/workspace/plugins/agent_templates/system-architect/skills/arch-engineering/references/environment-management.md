# 环境管理

## 配置层级

```
优先级（高 → 低）：
1. 命令行参数 / 环境变量（运行时注入）
2. Secrets Manager / Vault（敏感配置）
3. 环境配置文件（.env.staging / .env.prod）
4. 默认值（代码内安全默认）

原则：
✅ 代码与配置分离（Twelve-Factor）
✅ 敏感配置走 Secrets Manager，不入代码/配置文件
✅ 环境变量在启动时校验，快速失败
✅ 不同环境使用不同配置值，不硬编码
```

## 环境变量管理

### 环境分层

| 环境 | 用途 | 配置来源 | 数据 |
|------|------|---------|------|
| local | 本地开发 | .env.local | 假数据/本地 DB |
| dev | 开发联调 | .env.dev / CI 注入 | 共享开发数据 |
| staging | 预发布验证 | Secrets Manager | 生产脱敏数据 |
| prod | 生产 | Secrets Manager | 真实数据 |

### 配置校验

```typescript
// 启动时校验所有必需配置
function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required env var: ${name}`);
  return value;
}

function intEnv(name: string, defaultValue: number): number {
  const value = process.env[name];
  if (!value) return defaultValue;
  const parsed = parseInt(value, 10);
  if (isNaN(parsed)) throw new Error(`Invalid int for ${name}: ${value}`);
  return parsed;
}

const config = {
  port: intEnv('PORT', 3000),
  database: {
    url: requiredEnv('DATABASE_URL'),
    poolSize: intEnv('DB_POOL_SIZE', 10),
  },
  auth: {
    jwtSecret: requiredEnv('JWT_SECRET'),
    expiresIn: process.env.JWT_EXPIRES_IN || '15m',
  },
  cors: {
    origins: requiredEnv('CORS_ORIGINS').split(','),
  },
} as const;
```

### .env.example 规范

```bash
# .env.example — 提交到版本控制，假值
PORT=3000
DATABASE_URL=postgresql://user:pass@localhost:5432/myapp_dev
DB_POOL_SIZE=10
JWT_SECRET=replace-with-real-secret-in-prod
JWT_EXPIRES_IN=15m
CORS_ORIGINS=http://localhost:3000,http://localhost:3001
REDIS_URL=redis://localhost:6379
LOG_LEVEL=debug
```

```
✅ 提交 .env.example（假值，供新成员参考）
✅ .gitignore 排除 .env / .env.local / .env.prod
✅ 生产密钥通过 Secrets Manager 注入

❌ 不提交真实 .env 文件
❌ 不在代码中硬编码环境特定值
❌ 不在日志中打印环境变量值
```

## CORS 配置

### 基本配置

```typescript
import cors from 'cors';

const allowedOrigins = config.cors.origins;  // 从环境变量读取

app.use(cors({
  origin: (origin, callback) => {
    // 允许无 origin 的请求（Postman、curl、同源）
    if (!origin) return callback(null, true);
    if (allowedOrigins.includes(origin)) return callback(null, true);
    callback(new Error(`CORS: origin ${origin} not allowed`));
  },
  credentials: true,           // 允许携带 Cookie
  methods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
  allowedHeaders: ['Content-Type', 'Authorization'],
  exposedHeaders: ['X-Request-ID'],
  maxAge: 86400,               // 预检缓存 24h
}));
```

### CORS 规则

```
✅ 显式配置允许来源（从环境变量读取）
✅ 生产环境不用 '*'（特别是 credentials: true 时）
✅ 预检缓存（maxAge）减少 OPTIONS 请求
✅ 暴露自定义 Header（X-Request-ID 等）

❌ 不用 origin: '*' + credentials: true（浏览器拒绝）
❌ 不在 CORS 中放敏感 Header
❌ 不忽略 CORS 错误（静默失败）
```

### 常见 CORS 问题

| 问题 | 原因 | 修复 |
|------|------|------|
| 预检失败（OPTIONS 403） | allowedOrigins 不含请求来源 | 添加来源到环境变量 |
| Cookie 不发送 | credentials 未设 true | `credentials: true`（前后端都要） |
| 前端跨域读不到 Header | Header 未在 exposedHeaders | 添加到 exposedHeaders |
| 预检请求过多 | maxAge 未设或太短 | 设置 maxAge: 86400 |
| 生产仍用 '*' | 配置未更新 | 从环境变量读取允许来源 |

## 多环境策略

### 环境隔离原则

```
✅ 每个环境独立数据库（不共享）
✅ 每个环境独立密钥（不跨环境复用）
✅ Staging 配置尽可能接近 Prod（同构）
✅ Prod 配置变更走审批流程
✅ 环境间数据不手动同步（用迁移脚本）

❌ 不在 Dev/Staging 使用 Prod 数据（合规风险）
❌ 不跨环境共享密钥
❌ 不手动修改 Prod 配置
```

### 配置同步

```
配置变更流程：
1. 修改 .env.example（版本控制）
2. 更新 Secrets Manager（运维操作）
3. Staging 验证配置生效
4. Prod 部署时注入新配置
5. 验证应用启动（健康检查通过）
```

## 日志级别管理

| 环境 | LOG_LEVEL | 原因 |
|------|-----------|------|
| local | debug | 开发需要详细日志 |
| dev | debug | 联调需要排查 |
| staging | info | 接近生产，减少噪音 |
| prod | info | 生产只记必要信息 |

```
✅ 日志级别通过环境变量控制
✅ 生产不输出 debug 日志（性能 + 安全）
✅ 敏感字段在日志层脱敏（中间件统一处理）
❌ 不在生产用 console.log / print
```
