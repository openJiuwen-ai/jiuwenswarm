# Code Graph 代码检索

Code Graph 给 Coding Agent 一套基于仓库索引的检索工具（`find_*`），用来在改代码之前定位符号、调用关系和文件结构。默认关闭，行为与原来的 grep / read / edit 一致。

索引按 **canonical 绝对路径** 共享：同一 `project_dir` 的多个对话共用一张当前图，不按对话 fork。不同 clone / worktree 因真实路径不同而各有一张图。对话关闭只释放引用，不删除共享索引。

`profile` 决定开不开图；`agent` 决定挂在谁身上。Plan、Explore 不会获得这些工具。

只改 yaml **不够**。`graph` 还依赖 `tree-sitter-language-pack`。`uv sync` 不会装这个包，需要自己装：

```bash
uv pip install tree-sitter-language-pack
```

语法在随后的 `jiuwenswarm-init` / `jiuwenswarm-start` 里下载，不会拖到 Coding Agent 对话里。`profile: off`（仓库模板默认）不挂图工具、不建索引、不藏 grep，Coding Agent 就是原来的 grep / read / edit。关掉之后**不会**去刷新或作废已经建好的图，图只是闲置。`/status` 在 `off` 时显示 `absent`，避免看起来还在用图；磁盘 checkpoint 还在，再打开 `graph` 才检查这期间文件有没有变。装不上 parser 或注册失败时也不挂 `find_*`，工具表与关闭时相同。仓库超过上限（含后来新增太多文件，或建图时内存/磁盘超了）时 `UNAVAILABLE`：清掉旧图、恢复 grep，并提示抬对应的文件数、源码字节、内存或磁盘上限。图能完整建索引时会去掉 grep / glob。

选了 `profile: graph` 并且会话已经有 `project_dir` 时，**对话一开始就后台建图**，不必等第一次 `find_*`。进程启动时尚无项目路径，没法提前建。

## 如何打开

编辑产品配置（仓库内默认文件：`jiuwenswarm/resources/config.yaml`；本机运行常见路径：`~/.jiuwenswarm/config/config.yaml`）：

```yaml
code_graph:
  profile: "graph"   # off = 原版工具；graph = find_* 检索
  agent: "root"      # root（产品 yaml 默认）或 code_agent
  max_files: 5000
  max_source_bytes: 41943040   # 40MB
  max_build_rss_mb: 4096       # 进程内存
  max_cache_size_mb: 2048      # 索引磁盘
```

文件数和源码体积决定这个仓能不能进图。内存和磁盘是建图/更新过程中的硬停：测到真实占用超了就立刻停、清图、恢复 grep。进了上限的仓会等到**新图**建完，不会因为时间到了就放弃。符号数、边数、估算字节不是停图条件。

超过文件数或源码字节，或内存/磁盘不够：先丢掉**本仓旧图**。还不够时，再按创建时间从旧到新丢掉别的仓；有窗口正在读或正在建的不删。清完仍不够，才恢复 grep，并提示抬对应上限。保存配置后热更新：抬了上限会重新建图；没抬就一直用 grep。同一 `project_dir` 的多个对话共用这一张当前图。

同一 `project_dir` 的对话共享一份图。IDE / Shell 改文件由 watcher + 查询前校验追上。`/status` 用同一套 token：磁盘已经变了但还没刷新时显示 `stale`，不把旧图当成最新。版本目录、依赖目录、覆盖率报告、打包产物、前端构建缓存等通用生成目录默认不进索引。手册类文本（`.md` / `.rst` / `.txt`）默认不进索引；`search_source_text` 仍检索函数正文。单个文件超过 1MB 会跳过该文件。

实例配置（`~/.jiuwenswarm-instances/<name>/config/config.yaml`）只在 `jiuwenswarm-init` 时从仓库模板拷一次。之后改仓库模板不会自动覆盖已有实例；要对齐上限需要手改，或 `jiuwenswarm-init -f --name <name>`。

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

测试人员跑 ContextBench 用 `scripts/eval/`，会注入 locate 考试提示并挂上 `submit_code_context` 以产出 `<PATCH_CONTEXT>`。那不是产品用户路径，**不能直接套产品 yaml**。ContextBench 源码和 gold parquet **不在本仓库**；测试机设置 `CONTEXTBENCH_ROOT`（或把 ContextBench clone 成 `jiuwenswarm` 的兄弟目录 `../ContextBench`），不要依赖某台机器上的 `reconstruct_tmp`。

- 任务不同：评测是 locate 考试（提交上下文），产品是定位后 `edit_file` / 跑测试。
- 藏工具不同：评测还要藏 `bash` / `edit_file` / `write_file`（`--graph-agent root` 时再藏 `task_tool`），否则 Root 会不调图就交卷。产品只在图可用时藏 `grep` / `glob`；图 `UNAVAILABLE` 时加回来。评测失败不得回退 grep，否则污染 ablation。
- 默认挂载点不同：评测 `--profile graph` 默认仍挂 `code_agent`（与产品 yaml 模板的 `agent: root` 不同），这样此前实验数字仍然可比。

说明见 `scripts/eval/README.md`。

## 相关文档

- 全景：[Coding Agent](CodingAgent.md)
- 配置面板：[配置信息](配置信息.md)
