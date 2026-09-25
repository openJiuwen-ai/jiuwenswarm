# 🔄 五步闭环流程

## 🔄 五步闭环流程

### Phase 1：选题策划

**选题类型：**

| 类型 | 触发场景 | 输出 | 时效 |
|------|---------|------|------|
| 热点追踪 | 行业热点/节日 | 时效性内容 | 24h |
| 产品推广 | 新品/爆品 | 种草内容 | 常规 |
| 案例分享 | 客户案例 | 信任背书 | 常规 |
| 知识科普 | 智能家居知识 | 教育内容 | 常规 |
| GEO优化 | AI搜索优化 | 问答内容 | 常规 |

---

### Phase 2：RAG检索

**检索命令：**

```bash
# IMA知识库检索
@ima-skill:knowledge-base
  操作：搜索
  关键词：[主题]
  输出：历史爆款+产品信息+案例库
```

---

### Phase 3：内容创作

**创作命令：**

```bash
# 内容工厂
@content-factory
  topic: [主题]
  platform: [xiaohongshu/douyin/wechat]
  style: [warm/professional/funny]
  length: [字数]
  useRag: true
  输出：多平台内容

# AI痕迹检测
@anti-ai-slop
  输入：[生成内容]
  输出：检测报告+改写建议
```

---

### Phase 4：封面设计

**设计命令：**

```bash
# 社交卡片生成
@guizang-social-card-skill
  输入：[标题+副标题]
  平台：[公众号/小红书/视频号]
  输出：封面图PNG
```

---

### Phase 5：多平台分发

**分发命令：**

```bash
# 抖音发布
@douyin-upload
  输入：[视频/图文]
  标题：[标题]
  标签：[话题标签]

# 小红书发布
@xiaohongshu-upload
  输入：[图文]
  标题：[标题]
  标签：[话题标签]

# B站发布
@bilibili-upload
  输入：[视频]
  标题：[标题]
  标签：[话题标签]

# 快手发布
@kuaishou-upload
  输入：[视频/图文]
  标题：[标题]
```

---


