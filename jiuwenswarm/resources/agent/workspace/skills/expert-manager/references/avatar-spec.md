# 头像规范

## 一、两种放置方式

### 单专家（agent_template）

- 位置：`<expert_id>/avatars/<expert_id>.png`
- manifest 声明：`metadata.avatar: "avatars/<expert_id>.png"`
- 校验：`expert_store.validate_expert_package` 会检查文件存在 + 包内相对路径

### 专家团（agent_group）

- 位置：`<expert_id>/avatars/<leader_id_or_member_id>.png`
  （leader 固定 `avatars/leader.png`，成员 `avatars/<member>.png`）
- **无需 manifest 声明**：`agent_group.read_group_members` 隐式扫描
  `avatars/<id>.png`，存在则附绝对路径给前端

## 二、基本要求

| 项目 | 要求 |
|------|------|
| 格式 | PNG（推荐）或 JPG |
| 尺寸 | 512×512 px（正方形） |
| 大小 | 单张不超过 500KB |
| 风格 | 统一漫画/插画风格，专业自然 |
| 内容 | 符合角色定位，不含违规内容 |

> 使用 `image_gen` 工具生成，`size` 参数设为 `"1024x1024"`（高清大图，市场缩放为 512×512 展示）。

## 三、生成策略

**Agent 型**：1 张头像
- `avatars/<expert_id>.png`

**Team 型**：N+1 张头像
- `avatars/leader.png` — 主理人头像
- `avatars/<member>.png` — 每个团员头像

---

## 四、Prompt 构建核心原则

**每个头像的 prompt 必须从对应的 persona 文件描述中提取角色特征，不使用通用模板硬编码。**

### 提取步骤

1. **读取 persona 文件**
2. **提取角色身份**：从标题和首段提取
3. **提取专业特征**：从"核心能力"章节提取关键词，转化为视觉元素
4. **推断工作风格**：从"工作流程"和"注意事项"推断性格气质
5. **推断人物属性**：从 name 字段推断性别和风格基调

---

## 五、个人头像 Prompt 组装

```
[风格前缀] + [角色身份] + [外观特征] + [表情气质] + [背景元素] + [质量后缀]
```

| 部分 | 说明 | 示例 |
|------|------|------|
| 风格前缀 | 统一漫画/插画风格 | `Professional cartoon-style illustration avatar,` |
| 角色身份 | 从 persona 标题/首段提取 | `a female design system document architect` |
| 外观特征 | 从核心能力推断穿着/配饰 | `wearing stylish glasses, holding a design specification document` |
| 表情气质 | 从工作风格推断 | `confident and meticulous expression` |
| 背景元素 | 从专业领域提取视觉符号 | `subtle design tokens and color palette swatches in background` |
| 质量后缀 | 固定 | `Bust shot, facing forward. Clean simple background. High quality, professional, natural.` |

### 示例 1：设计系统架构师

persona 核心内容：角色=设计系统文档架构师，能力=9大标准章节、AI可读格式，输出=Markdown+HEX+CSS

```
Professional cartoon-style illustration avatar, a female design system document architect,
wearing stylish glasses, holding a design guideline document, modern creative smart casual attire,
confident and meticulous expression with a creative yet precise aura,
subtle color palette swatches, typography samples and design token symbols in the background.
Bust shot, facing forward. Clean simple warm-toned background. High quality, professional, natural.
```

### 示例 2：技术分析师

persona 核心内容：角色=技术分析师，能力=K线形态、均线分析、MACD/RSI/KDJ

```
Professional cartoon-style illustration avatar, a male technical stock market analyst named Marco,
wearing a sharp vest over dress shirt, looking at holographic candlestick charts,
focused and analytical expression with sharp observant eyes,
K-line charts, moving average lines and MACD indicators floating in the background.
Bust shot, facing forward. Clean simple blue-toned background. High quality, professional, natural.
```

---

## 六、同一团队风格统一规则

Team 型的所有头像必须在 prompt 中保持一致的**风格锚定词**：

**固定风格前缀（每个 prompt 开头）：**
```
Professional cartoon-style illustration avatar, consistent art style with warm lighting and soft shadows,
```

**固定质量后缀（每个 prompt 结尾）：**
```
Bust shot, facing forward. Clean simple {color}-toned background. High quality, professional, natural.
```

### 背景色调（按团队领域自选，非强制）

| 领域 | 建议背景色调 |
|------------|---------|
| 产品设计 | warm orange-coral |
| 技术工程 | blue-purple |
| 游戏空间 | purple-red gradient |
| 数据智能 | cyan-teal |
| 营销增长 | red-orange |
| 内容创作 | pink-magenta |
| 销售商务 | golden-amber |
| 金融投资 | dark blue with gold accent |
| 运营人力 | navy slate-blue |
| 项目质量 | green-emerald |
| 法务安全 | dark grey-blue |
| 行业顾问 | deep teal with silver accent |

> 同一团队所有头像使用**相同**的背景色调，保证视觉一致性。

---

## 七、执行流程

1. **读取 persona** — 逐个读取每个角色的 persona 文件
2. **提取角色特征** — 从角色定义、核心能力、工作流程中提取
3. **构建个人 prompt** — 按上述步骤将特征转化为视觉描述
4. **统一风格锚定** — 确保所有 prompt 使用相同的风格前缀和后缀
5. **调用 image_gen** — 逐张生成，输出目录为专家包的 `avatars/`，`size` 为 `"1024x1024"`
6. **重命名文件** — 将生成的图片重命名为对应成员 id（单专家用 `<expert_id>.png`，团 leader 用 `leader.png`，成员用 `<member>.png`）
7. **验证** — 确认所有头像文件已存在于 `avatars/`
8. **单专家补 manifest** — 在 `metadata.avatar` 声明相对路径（专家团无需）

## 八、注意事项

1. **必须基于 persona 描述生成**：不要使用通用 prompt
2. **团队头像画风一致**：共用风格锚定词和背景色调
3. **正面半身**：职业感、可识别、中性背景
4. **避免文字**：头像内不要出现 logo/水印文字
5. **生成失败处理**：在 README.md 中标注需手动补充的头像，附推荐 prompt

## 九、替换

用户可手动替换为自定义头像，只要文件名、尺寸、格式符合上述规格即可：
- 单专家替换后无需改 manifest（路径不变）
- 专家团替换后被 `read_group_members` 自动拾取（隐式扫描）
