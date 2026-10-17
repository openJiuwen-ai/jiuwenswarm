# tool 规范（tools/<name>_tool.py）

继承 `openjiuwen.core.foundation.tool.Tool`，通过 `ToolCard` 描述能力。何时选用见 `fill-package.md` §2。

## 基类骨架

```python
from openjiuwen.core.foundation.tool import Tool, ToolCard


class MyTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="my_tool",
                name="my_tool",
                description="工具描述：何时用、做什么",
                input_params={
                    "type": "object",
                    "properties": {
                        "param1": {"type": "string", "description": "参数1描述"},
                    },
                    "required": ["param1"],
                },
            )
        )

    async def invoke(self, inputs, **kwargs):
        return {"success": True, "result": "..."}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
```

## Tool 实现约束

- **无参构造**：`__init__(self) -> None`；在内部自建 `ToolCard`
- `ToolCard.id` **与** `ToolCard.name` **都必须显式设置**；推荐二者一致、snake_case
- `description` **写清触发场景**，便于模型在普通 query 中主动调用；若配合 Skill，写明「需配合  skill 使用」
- `input_params` **必须是合法 JSON Schema（**`type: "object"`**）**：
  - 即使无参数，也必须写 `input_params={"type": "object", "properties": {}}`
  - 禁止省略 `input_params`，禁止写成空字典 `{}`（会触发 API：`schema must be a JSON Schema of 'type: "object"'`）
  - `type` 必须是字符串 `"object"`，不能缺失或为 `null`
  - 有参数时，`properties` 里每个参数都要有 `type` 和 `description`；`required` 列出必填名
- **禁止**通过构造函数注入 Rail / agent / session / workspace 等运行时对象
- 若需读取 Rail 采集的数据：经**文件系统状态**交互（按 session 隔离），不要扫 agent.rails 或读 Rail 实例字段
- 拿不到真实数据时：返回明确降级字段（如 `estimated: true`、`source: "estimated"`）或错误说明
- 文件/产物生成：`success=true` 前自校验路径存在、`size_bytes > 0`、format 与后缀一致；返回 `path`/`absolute_path`、`exists`、`format`、`size_bytes` 等
- 依赖缺失、写入失败、格式校验失败：必须 `success=false` + 明确错误；不得返回成功文本
- 不得用 JSON/Markdown/纯文本冒充 PPTX/DOCX/PDF 等最终产物

## 运行时路径规则

- 涉及文件写入的 Tool，写入路径必须由入参显式传入（如 `output_dir`），且只能是用户指定目录或当前项目目录
- **禁止自行推导写入路径**
- 内部状态不适用本节，见「与 Rail 协作」；包内资源路径只用于读取模板/素材

## 与 Skill 协作

- Tool 是执行层，不独立包办复杂全流程（品牌适配、模板选择、内容规划等由 Skill 分步指导）
- 禁止在单个 `invoke` 中完成应由 Skill 指导的全部环节
- 成对出现时 manifest 同时声明 `skills[]` 与 `tools[]`；skill 正文写清何时调用哪个 tool、调用顺序

## 与 Rail 协作

- Rail 监听生命周期、维护 session 状态；Tool 读同一状态返回结构化结果，字段名稳定
- 状态根使用 `get_workspace()`，按 session 隔离；未配置 workspace 时明确报错；原子写（临时文件 + `os.replace`）
- Tool 从 `kwargs["session"].get_session_id()` 取 session
- 模块级全局变量只能作可丢弃缓存，不能作为 Rail/Tool 共享状态的事实来源

## 关键约束

1. `from openjiuwen.core.foundation.tool import Tool, ToolCard`
2. 类名 PascalCase，文件名 snake_case；manifest `tools[].class` = 类名
3. `invoke` 捕获异常，返回失败 dict，勿抛裸异常

## 代码质量要求

参考 `@references/code-quality.md`。
