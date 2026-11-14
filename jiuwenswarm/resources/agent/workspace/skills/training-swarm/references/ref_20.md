# 🔗 A2A 接口 (v1.0)

## 🔗 A2A 接口 (v1.0)

> 本兵种代号: `training-swarm` | 角色: 辅学助手兵

### 可接收的Intent

| Intent | 来源兵种 | 处理方式 |
|--------|---------|---------|
| `review_request` | content-swarm | 接收内容，执行质量审核 |
| `data_share` | quote-swarm | 接收产品信息，更新知识库 |
| `data_share` | followup-swarm | 接收案例素材，归档到知识库 |

### 可发送的Intent

| Intent | 目标兵种 | 触发时机 | payload结构 |
|--------|---------|---------|------------|
| `review_response` | content-swarm | 审核完成返回结果 | `{original_task_id, status: "approved/rejected", review: {...}}` |
| `data_share` | quote-swarm | 知识库更新产品信息 | `{data_type: "knowledge", data: product_update}` |
| `alert` | 所有兵种 | 新人考核不通过 | `{alert_type: "training_gap", target: "新人员工"}` |

### 消息处理流程

```
收到A2A消息 →
  1. 校验 a2a_version == "1.0.0"
  2. 检查 intent 是否在可接收列表中
  3. 如果是 review_request → 执行5维度质检
  4. 如果是 data_share → 归档到知识库
  5. 构造 review_response / data_share → 发送到目标兵种
```

---


