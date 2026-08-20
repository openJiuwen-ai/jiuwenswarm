# Code Graph 代码检索

Code Graph 给 Coding Agent 一套基于仓库索引的检索工具（`find_*`），用来在改代码之前定位符号、调用关系和文件结构。默认关闭，行为与原来的 grep / read / edit 一致。

图工具只挂在 **Code Agent** 上。Root、Plan、Explore 不会获得这些工具。

## 如何打开

编辑产品配置（仓库内默认文件：`jiuwenswarm/resources/config.yaml`；本机运行常见路径：`~/.jiuwenswarm/config/config.yaml`）：

```yaml
code_graph:
  profile: "graph"   # off = 原版工具；graph = find_* 检索
  max_files: 50000
  max_index_size_mb: 1024
  query_timeout_seconds: 10
```

`profile` 是唯一开关。只接受 `off` 或 `graph`；其它值当作 `off`。

## 打开后能做什么

定位已知类/函数：`resolve_symbol` → `read_symbol`。

不知道精确名：`find_code_symbols` 生成候选，再 `read_symbol`。

精确字面量（报错、配置键、decorator）：`search_source_text`。

文件或类结构：`inspect_code_structure`。

邻居关系：`find_callers` / `find_callees` / `find_importers` / `find_base_classes` / `find_subclasses`。

多跳调用链：`trace_call_paths`（必须传 `direction`）。

定位完成后，同一个 Code Agent 继续 `edit_file` / `write_file` 并跑测试。产品模式**没有** `submit_code_context`。

## 和评测脚本的区别

测试人员跑 ContextBench 用 `scripts/eval/`，会注入 locate 考试提示并挂上 `submit_code_context` 以产出 `<PATCH_CONTEXT>`。那不是产品用户路径。说明见 `scripts/eval/README.md`。

## 相关文档

- 全景：[Coding Agent](CodingAgent.md)
- 配置面板：[配置信息](配置信息.md)
