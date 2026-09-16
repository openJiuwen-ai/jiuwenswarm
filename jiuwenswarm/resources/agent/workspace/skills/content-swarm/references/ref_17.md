# 🔗 A2A 接口 (v1.0)

## 🔗 A2A 接口 (v1.0)

> 本兵种代号: `content-swarm` | 角色: 内容生成兵

### 可接收的Intent

| Intent | 来源兵种 | 处理方式 |
|--------|---------|---------|
| `data_share` | quote-swarm | 接收报价数据，生成案例内容 |
| `data_share` | followup-swarm | 接收客户画像，生成跟进话术 |
| `review_request` | training-swarm | 接收内容质量审核请求 |

### 可发送的Intent

| Intent | 目标兵种 | 触发时机 | payload结构 |
|--------|---------|---------|------------|
| `task_delegation` | design-swarm | 需要封面图/产品图 | `{task_type: "cover_design", context: {title, platform}}` |
| `review_request` | training-swarm | 内容完成，需质量审核 | `{content_type: "article", content: draft}` |
| `result_return` | quote-swarm | 案例内容完成 | `{original_task_id, status: "completed", output: {articles}}` |

### 消息处理流程

```
收到A2A消息 →
  1. 校验 a2a_version == "1.0.0"
  2. 检查 intent 是否在可接收列表中
  3. 从 payload.data 提取内容素材
  4. 调用 content-factory 生成内容
  5. 执行 anti-ai-slop 检测
  6. 如果需要封面 → 构造 task_delegation → design-swarm
  7. 构造 result_return → 发送到触发兵种
```

---


