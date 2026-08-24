# Code Graph 代码检索

Code Graph 给 Coding Agent 一套基于仓库索引的检索工具（`find_*`），用来在改代码之前定位符号、调用关系和文件结构。默认关闭，行为与原来的 grep / read / edit 一致。

`profile` 决定开不开图；`agent` 决定挂在谁身上。Plan、Explore 不会获得这些工具。

只改 yaml **不够**。`graph` 还依赖 `tree-sitter-language-pack`。`uv sync` 不会装这个包，需要自己装：

```bash
uv pip install tree-sitter-language-pack
```

语法在随后的 `jiuwenswarm-init` / `jiuwenswarm-start` 里下载，不会拖到 Coding Agent 对话里。装不上或下载失败时，工具会挂上，但建索引返回 `UNAVAILABLE`，**保留（或立刻恢复）grep / glob**。图能建索引时会先去掉 grep / glob，避免模型绕开索引。

## 如何打开

编辑产品配置（仓库内默认文件：`jiuwenswarm/resources/config.yaml`；本机运行常见路径：`~/.jiuwenswarm/config/config.yaml`）：

```yaml
code_graph:
  profile: "graph"   # off = 原版工具；graph = find_* 检索
  agent: "root"      # root（产品 yaml 默认）或 code_agent
  max_files: 50000
  max_index_size_mb: 1024
  query_timeout_seconds: 10
```

`profile` 只接受 `off` 或 `graph`；其它值当作 `off`。

`agent` 只接受 `root` 或 `code_agent`。产品模板默认是 `root`：只把 `profile` 改成 `graph` 即可，不必打开 `code_agent`。配置里没有写 `agent` 时，仍挂在 `code_agent` 上（和此前评测默认一致）。挂在 `code_agent` 时需要 `react.subagents.code_agent.enabled: true`。

## 打开后能做什么

定位已知类/函数：`resolve_symbol` → `read_symbol`。

不知道精确名：`find_code_symbols` 生成候选，再 `read_symbol`。

精确字面量（报错、配置键、decorator）：`search_source_text`。

文件或类结构：`inspect_code_structure`。

邻居关系：`find_callers` / `find_callees` / `find_importers` / `find_base_classes` / `find_subclasses`。

多跳调用链：`trace_call_paths`（必须传 `direction`）。

定位完成后，同一个挂载点继续 `edit_file` / `write_file` 并跑测试。产品模式**没有** `submit_code_context`。

## 和评测脚本的区别

测试人员跑 ContextBench 用 `scripts/eval/`，会注入 locate 考试提示并挂上 `submit_code_context` 以产出 `<PATCH_CONTEXT>`。那不是产品用户路径，**不能直接套产品 yaml**：

- 任务不同：评测是 locate 考试（提交上下文），产品是定位后 `edit_file` / 跑测试。
- 藏工具不同：评测还要藏 `bash` / `edit_file` / `write_file`（`--graph-agent root` 时再藏 `task_tool`），否则 Root 会不调图就交卷。产品只在图可用时藏 `grep` / `glob`；图 `UNAVAILABLE` 时加回来。评测失败不得回退 grep，否则污染 ablation。
- 默认挂载点不同：评测 `--profile graph` 默认仍挂 `code_agent`（与产品 yaml 模板的 `agent: root` 不同），这样此前实验数字仍然可比。

说明见 `scripts/eval/README.md`。

## 相关文档

- 全景：[Coding Agent](CodingAgent.md)
- 配置面板：[配置信息](配置信息.md)
