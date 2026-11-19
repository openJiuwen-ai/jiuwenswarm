# 视觉来源路由

视觉选择的目标是提高证据价值和表达效率，不是保证每页都有图片。

## 可执行契约

所有非临时视觉先写入项目 `evidence-plan.json`，再运行 `prepare_evidence.py`。`design-spec.md` 解释为什么需要，evidence plan 记录怎样获取和当前事实状态，execution lock 只保存逐页允许使用的 `asset_ids`。

典型论文项：

```json
{
  "id": "reasoningbank-architecture",
  "page": "P08",
  "claim": "ReasoningBank 通过共享记忆支持多 Agent 迁移",
  "purpose": "用论文原始架构图证明记忆检索和协作关系",
  "kind": "paper-figure",
  "acquire_via": "paper-figure",
  "required": true,
  "placement_role": "evidence",
  "status": "planned",
  "path": "assets/papers/reasoningbank-architecture.png",
  "source_path": null,
  "source_url": "https://arxiv.org/pdf/xxxx.xxxxx",
  "source": "Paper title, authors, Figure 2",
  "license": "Paper figure quoted for analysis; retain citation",
  "reference": "Figure 2 architecture, not a recreated approximation",
  "query": null,
  "crop_policy": "no-crop",
  "paper_selector": {
    "label": "Figure 2",
    "caption_keywords": ["ReasoningBank", "memory"],
    "include_caption": false,
    "min_confidence": 0.55
  },
  "review": { "status": "pending", "notes": null },
  "result": null,
  "fallback": "Use the full PDF page with citation or draw a clearly labeled interpretation."
}
```

准备脚本复用 `extract_arxiv_visuals_v2_2.py`，不会重新实现裁图。自动选择不唯一时保留 manifest 和 contact sheet，状态转为 `needs-manual`。

```bash
python3 ../scripts/prepare_evidence.py .
# 查看 analysis/evidence-contact-sheet.jpg 或论文专属 contact sheet 后：
python3 ../scripts/prepare_evidence.py . --approve reasoningbank-architecture
# 素材真正加入幻灯片后：
python3 ../scripts/prepare_evidence.py . --used reasoningbank-architecture
```

文件视觉只有经过 `review.approved` 才能标为 `ready/used`。原生图表和原生绘制不需要图像审核，但仍需在交付前标为 `used`。

## 六种路径

### user-material

适用：用户材料已有照片、图表、截图、品牌视觉或产品界面。

- 优先保持原文件和语义；
- 检查分辨率、比例、裁切和使用范围；
- 不因为已有组件更方便就替换真实材料。

### paper

适用：页面解释一篇具体论文的机制、架构或实验结果，原图能直接作为证据。

- 可使用 `../scripts/extract_arxiv_visuals_v2_2.py` 提取；
- 默认由 `prepare_evidence.py` 的 `paper-figure` 路由调用该脚本并解析 manifest；
- 根据图注、尺寸和页面核心信息选择，不按置信度机械排序；
- 先确认可读性，再决定使用 1–3 张或改为自绘；
- 原图保持比例和内部视觉，外围使用本 deck 模板语言；
- 在 `evidence-plan.json` 中记录论文来源；**不在幻灯片上画来源行**，除非用户明确要求。

### web

适用：真实人物、地点、产品、机构、新闻事件或企业场景。

- 搜索具体实体或场景，不用泛化关键词接受错误图片；
- 验证主体、时间、来源和许可；
- 需要署名时在页面内提供简洁署名；
- 无法验证则使用占位、用户 URL 或其他表达路径。

### native-drawing

适用：流程、机制、架构、因果、关系、分类、抽象框架，需要可编辑和语义清晰。

- 可直接使用 PptxGenJS、原生图表/表格或自定义 SVG；
- 图形结构从内容关系推导，不从组件外形反推内容；
- 只有理解关系需要的节点和连线才画出来。

### component

适用：现有组件与页面信息结构高度吻合，且使用组件不会迫使内容删改或变形。

- 先完成设计判断，再打开组件文档；
- 可以组合、改造或只使用组件的一部分；
- 组件不匹配时切换到 native-drawing，不修改内容迎合 API。

### none

适用：核心信息通过文字、数字或引语表达最清晰。

- 允许没有图形组件；
- 通过字号、位置、分隔和对齐建立层级；
- 文字仍需填满内容区，不得靠空白撑版面；
- 不添加装饰图标或无证据视觉充数。

## 决策问题

逐页回答：

1. 页面是在证明事实、解释关系、展示实体还是强调结论？
2. 真实原始视觉是否比重绘更有证据价值？
3. 可编辑性是否重要？
4. 外部素材是否可验证、可授权、可读？
5. 某组件是否真正匹配内容关系？
6. 不使用视觉是否反而更强？

## 回退示例

| 首选 | 失败原因 | 回退 |
|---|---|---|
| paper | 图太复杂或提取残缺 | 根据论文自绘并注明整理来源 |
| web | 实体或许可无法验证 | 用户素材、占位或文字表达 |
| component | API 迫使结构失真 | native-drawing |
| native-drawing | 内容缺少可靠关系 | 文字、表格或补充调研 |
| user-material | 清晰度不足 | 请求原图、重新绘制或标注限制 |

所有失败和替代都记录到 `design-spec.md` 或素材清单，不静默编造。
