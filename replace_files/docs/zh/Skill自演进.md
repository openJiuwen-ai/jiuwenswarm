# Skill 自演进

Agent 系统普遍存在一个问题：能力定义一旦写好，就基本不会再变了。工具调用出错，记录一条日志；用户反馈说理解有误，下次还是同样的逻辑。能力的上限，从部署那天就已经固定了。

JiuwenClaw 基于 **openJiuwen 自演进框架**，以 `SkillCallOperator` 算子统一管理所有 Skills 的读写与演进分发。在此基础上，系统内置了一套演进信号检测机制，持续监听执行过程和对话内容，将真实使用中遇到的问题转化为 Skills 的改进输入。

## 核心组件

### SkillCallOperator

SkillCallOperator 是 JiuwenClaw 基于 openJiuwen 框架实现的 Operator 算子，负责 Skills 的统一管理。

作为 JiuwenClaw 与 Skills 交互的核心入口，它承担以下职责：
- 读取 Skill 定义（SKILL.md）
- 执行 Skill 指令
- 自动加载 Skill 积累的演进经验

当系统检测到需要改进的地方，这些改进会先存入 `evolutions.json`，SkillCallOperator 会把它们合并后一起返回给 Agent。这意味着每次调用 Skill 时，都能获取到最新的演进经验。

### SkillOptimizer

SkillOptimizer 是 JiuwenClaw 基于 openJiuwen 框架实现的 Optimizer 优化器，负责驱动整个 Skill 演进流程。

它的核心工作包括：
1. **接收信号**：从 SignalDetector 接收异常信号，理解当前 Skill 遇到了什么问题
2. **分析判断**：结合对话上下文，判断这个问题是否值得记录
3. **生成改进**：调用 LLM 生成具体的改进建议
4. **执行记录**：将生成的改进方案写入演进记录

当你使用 `/evolve` 命令时，背后就是 SkillOptimizer 在工作。

### SkillEvolutionManager

SkillEvolutionManager 是演进生命周期的核心管理者，负责协调各个阶段的演进工作：

- **信号扫描**：调用 SignalDetector 提取需要演进的事件
- **记录生成**：调用 LLM 将信号转化为可执行的改进方案
- **存储管理**：维护 `evolutions.json` 文件的读写
- **内容固化**：将待定演进记录合并到原始 SKILL.md

它衔接了 SignalDetector、SkillOptimizer 和 SkillCallOperator，形成完整的演进闭环。

### SignalDetector

SignalDetector 是演进信号的检测器，持续监听对话和执行结果中的异常。

它基于规则工作，不需要调用 LLM，因此响应速度快：
- 监听每一次工具执行的结果，捕捉错误关键词
- 捕捉用户的纠正反馈（如"不对"、"应该"等）
- 判断信号应该归到哪个 Skill 并关联上下文

---

## 识别哪些信号？

信号来源主要分两类：

### 执行异常

包括工具调用超时、接口返回报错、代码执行中的异常中断等。只要任务执行中出现明确的失败字样，系统会自动识别并将其归因到当前正在执行的 Skill 上。

检测关键词包括但不限于：
- 通用错误：`error`、`exception`、`failed`、`failure`、`timeout`
- 网络相关：`connection error`、`econnrefused`、`enoent`
- 其他：`permission denied`、`command not found`

### 用户纠错

当你说“不对”、“应该换个方式”、“你理解错了”这类话语时，系统不会将其当作普通对话略过，而是会识别为一次有效的负反馈。这类信号往往比报错日志更有价值——它直接点出了 Skill 在理解或处理逻辑上的偏差。

检测模式包括：
- 中文：`不对`、`不是这`、`错 了`、`应该 是`、`你搞错了`、`纠正一下`
- 英文：`that's wrong`、`you're wrong`、`should be`、`actually`

---

## 信号捕获之后做什么？

系统会全程追踪当前活跃的 Skill 模块，确保每个信号都能准确对应到具体的 Skill 文档。具体的改写逻辑如下：

### 异常案例 → 排障建议

执行失败的现场记录会被整理成具体的操作建议，补充进 Skills 的 `Troubleshooting`（已知问题与处理方式）部分。下次遇到相同场景，Skill 可以主动提示已知的风险点和应对方式。

```text
原始信号：
Tool 'weather-check' returned: Error: API timeout after 30s

演进为：
## Troubleshooting
- 遇到天气 API 超时错误时，优先检查网络连接，可考虑添加重试机制或降级策略。
```

### 纠错交互 → 示例补充

用户纠错的对话片段会作为新的 `Example`（正确用法示例）写入 Skills 文档，让后续的调用更容易理解用户的真实意图。

```text
原始信号：
User: 不对，我说的是查询上海不是北京

演进为：
## Examples
- 用户说"查询上海天气"时应调用上海的经纬度参数，而非默认北京
```

---

## 演进流程

```text
用户对话 / 工具执行
        │
        ▼
┌───────────────────┐
│  SignalDetector   │  监听并识别信号
│   检测执行异常     │
│   检测用户纠错     │
└────────┬──────────┘
         │
         ▼
┌─────────────────────────────┐
│    SkillEvolutionManager    │
│         .scan()            │  提取演进信号
└────────────┬───────────────┘
             │
             ▼
┌─────────────────────────────┐
│    SkillEvolutionManager    │
│       .generate()          │  LLM 生成演进记录
└────────────┬───────────────┘
             │
             ▼
┌─────────────────────────────┐
│      evolutions.json        │  写入待固化记录
│    (Skill 目录下)          │
└────────────┬───────────────┘
             │
             ▼ (可选)
┌─────────────────────────────┐
│         .solidify()         │  合并到 SKILL.md
└─────────────────────────────┘
```

---

## 演进文件

演进记录存储在每个 Skill 目录下的 `evolutions.json` 文件中：

```json
{
  "skill_id": "<skill_name>",
  "version": "1.0.0",
  "updated_at": "2024-01-15T10:30:00Z",
  "entries": [
    {
      "id": "ev_1234abcd",
      "source": "execution_failure",
      "timestamp": "2024-01-15T10:30:00Z",
      "context": "API timeout after 30s",
      "change": {
        "section": "Troubleshooting",
        "action": "append",
        "content": "## 常见问题\n- 遇到 API 超时错误时..."
      },
      "applied": false
    }
  ]
}
```

其中 `applied: false` 表示待固化状态，`applied: true` 表示已固化到 SKILL.md。
 
---

## 演进效果

这套机制让 Skills 不再是一次性的静态文档，而是随着真实使用持续迭代的活文档。不需要任何人工干预，智能体在日常运转过程中就完成了对自身的改进。

演进后的 Skill 在下次被调用时，会自动检查 Skill 目录下是否存在 `evolutions.json` 文件，存在时会自动加载演进经验的内容，从而：
- 主动提示已知的风险点和应对方式
- 更准确地理解用户的真实意图
- 持续优化自身的表现

---

## 如何使用

使用 JiuwenClaw 时，你可以通过以下方式与 Skill 演进功能交互：

### 自演进配置开关

skill 自动演进功能通过在配置信息中开启自演进配置项 `evolution_auto_scan` 开关启用。

![打开自演进自动检测](../assets/images/skill演进_自动检测开关.png)

### 自动演进（无需干预）

系统会在每次工具执行和对话结束后自动检测演进信号。当检测到执行异常或用户纠错时，会自动生成演进记录并存入 `evolutions.json`。

你无需做任何操作，演进在后台静默进行。下次调用该 Skill 时，会自动加载包含演进经验的内容。

![自动触发](../assets/images/skill演进_自动触发.png)

### 手动触发演进

如果希望立即为某个 Skill 触发演进，可以输入：

```bash
/evolve <skill_name>
```

例如：

```bash
/evolve xlsx
```

系统会扫描最近的对话和执行记录，为该 Skill 生成演进经验，并显示生成结果。

![手动触发](../assets/images/skill演进_手动触发.png)

### 查看演进状态

想知道哪些 Skill 有待固化的演进经验，可以输入：

```bash
/evolve list
```

系统会列出所有包含待演进记录的 Skill 及具体内容摘要。

![信息总览](../assets/images/skill演进_信息总览.png)

### 如何管理演进经验

演进经验存储在 Skill 目录下的 `evolutions.json` 文件中，你可以直接编辑该文件来管理演进经验。

**目录位置：**

```
~/.jiuwenclaw/workspace/agent/skills/<skill_name>/
├── SKILL.md           # Skill 源文档
├── evolutions.json    # 演进经验记录 ← 在这里编辑
└── ...
```

**演进记录示例：**

```json
{
  "entries": [
    {
      "id": "ev_1cdbc3a5",
      "source": "execution_failure",
      "timestamp": "2026-03-09T09:33:08Z",
      "context": "错误上下文",
      "change": {
        "section": "Troubleshooting",
        "action": "append",
        "content": "演进内容",
        "relevant": true
      },
      "applied": false
    }
  ]
}
```

**常用操作：**

- 添加新记录：在 `entries` 数组中追加新对象
- 修改记录：直接编辑对应字段
- 删除记录：从数组中移除
- 标记已固化：将 `applied` 设为 `true`

修改后在下次对话时会自动加载。