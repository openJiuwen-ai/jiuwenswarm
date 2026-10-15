# Experiment Schemas（兼容索引）

本文件为旧路径保留，避免历史链接失效；它不是模块三的字段规范。

当前唯一正式契约是 [references/experiment-schemas.md](references/experiment-schemas.md)。模块二投影、模块三执行、模块四消费和跨模块校验均以该文件及 `scripts/contracts.py` 为准。正式产物必须使用本次运行目录内的相对路径、保留真实执行与绘图源数据的 provenance，并通过 `PlanningFeedback` 返回不可安全执行的规划缺口。

如需修改接口，请同步修改正式契约、机器可读模型和对应校验代码；不要向本兼容索引添加字段。
