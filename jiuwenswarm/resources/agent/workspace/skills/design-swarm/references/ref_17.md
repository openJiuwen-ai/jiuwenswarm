# 🔗 A2A 接口 (v1.0)

## 🔗 A2A 接口 (v1.0)

> 本兵种代号: `design-swarm` | 角色: 设计技术兵

### 可接收的Intent

| Intent | 来源兵种 | 处理方式 |
|--------|---------|---------|
| `task_delegation` | quote-swarm | 接收设备清单，生成3D效果图 |
| `task_delegation` | content-swarm | 接收封面需求，生成封面图 |
| `data_share` | followup-swarm | 接收设计需求，定制方案 |

### 可发送的Intent

| Intent | 目标兵种 | 触发时机 | payload结构 |
|--------|---------|---------|------------|
| `result_return` | quote-swarm | 3D效果图完成 | `{original_task_id, status: "completed", output: {render_url}}` |
| `result_return` | content-swarm | 封面图完成 | `{original_task_id, status: "completed", output: {cover_png}}` |
| `result_return` | followup-swarm | 设计方案完成 | `{original_task_id, status: "completed", output: {design_files}}` |

### 消息处理流程

```
收到A2A消息 →
  1. 校验 a2a_version == "1.0.0"
  2. 检查 intent 是否在可接收列表中
  3. 从 payload.context 提取设计需求
  4. 调用 smart-home-wiring 或 smart-home-icons
  5. 生成3D效果图/布线图/封面
  6. 构造 result_return → 发送到触发兵种
```

---


