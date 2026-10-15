# Conception Schemas（兼容索引）

本文件为旧路径保留，避免历史链接失效；它不是模块一的字段规范。

当前唯一正式契约是 [references/conception-schemas.md](references/conception-schemas.md)。构思 Skill、`conception-agent`、规划输入校验和顶层编排均以该文件为准：构思模块使用每轮独立的原生 `research_agent`，正式输出包含恰好 10 篇可核验参考文献，且不得以会话记忆替代本轮检索证据。

如需修改接口，请同步修改正式契约、机器可读模型和相应校验代码；不要向本兼容索引添加字段。
