Issue #4689 阶段二、三、四交付说明
报告日期：2026-10-11（Asia/Shanghai）

先打开 issue4689-acceptance.html。它可以离线阅读，表格和结论完整写入 HTML。
解压完整证据包到同一目录后，报告中的相对链接可打开原始采样和测试摘要。

源码基线 develop：3fea8d1623a13646e935df9e96100ab7ad470aa4
最终性能测试代码：fc7c36c975543008d1c43e00db256cb80329846a
实施分支：feature/issue-4689-runtime-residency-20261010
现有依赖锁保持不变；产品改动只对应阶段二、三、四。

验收：Front / 控制面 / 已有 Worker 性能达标；冷 Worker 约 8 秒，未达 2.5 秒。
失败、既有回归失败和未覆盖的验证如实列在报告内。
没有推送分支或创建 MR。

材料：
- acceptance-results.json：两个模式、两个版本的 P50 / P95 与样本数。
- front-final-*.jsonl / chat-final-*.jsonl：完整正式样本及 perf_counter_ns 时间戳。
- *.environment.json / acceptance-manifest.json：测量环境、版本及依赖哈希。
- native-*.json：真实进程、工具、交互、隔离和 metadata 并发证据。
- summaries/：测试命令输出中的摘要；大体积 traceback 不打包。
- regression-baseline-difference.json：83 个候选失败在原始 develop 复现的逐项对照。
- lint-baseline-difference.json：相同修改文件的既有 Ruff 诊断对照。
- change-scope.csv：产品/测试源码改动范围。
- issue4689-implementation.patch：相对于上述 develop 的完整最终补丁。
- issue4689-implementation.bundle：本地提交与分阶段历史；需要已有 develop 基线。

在包含上述基线的 Git 仓库导入，不覆盖当前分支：
  git bundle verify <路径>/issue4689-implementation.bundle
  git fetch <路径>/issue4689-implementation.bundle feature/issue-4689-runtime-residency-20261010:review/issue-4689
  git switch review/issue-4689

测量/原生验证驱动位于 scripts/benchmarks/issue4689_*.py。
code.normal 与 agent 分别采集 30 次冷 Front、30 次冷 Worker、100 次已有 Worker。
真实 Gateway 场景请顺序运行，避免既有辅助服务固定端口冲突。
两种版本使用相同解释器及锁定环境，具体复现命令见 HTML。

原有工作区 D:\work_space\jiuwenswarm-process-cli-command-gap-20261008 和分析文件保留。
