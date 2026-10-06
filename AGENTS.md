# 协作约定

- 简短精简回答，说人话。
- 办公、开发数据集的双工 A2A 评测在远程服务器 `wzr@47.79.124.13` 执行，SSH 端口 `31442`，别名 `jiusi-vllm-evolve`。
- 已获用户授权在该服务器搭建评测环境并运行；不要因为本机缺 Docker 就停止。
- 用户明确要求使用现有服务器环境；不要再创建嵌套虚拟机。此前误建的 QEMU 实例和隧道已停止。
- 最新要求：直接用测试题运行，不把容器部署当作前置条件。题目改编与原题评分要如实区分。
- 评测资料与结果目录：`/workspace/wzr/duplex-benchmark-20260908/office-code-20260915`。具体命令和状态见 `docs/duplex-office-code-run.md`。
- 2026-09-17 用户将默认执行/纠错确认模型切为 `deepseek-v4.1-flash`（所给接口中的 DS v4.1）；远程私有配置 `/workspace/wzr/duplex-benchmark-20260908/private/models.json` 已更新。看护/路由为 `Qwen/Qwen3.5-9B`。密钥只存远程私有配置，勿写进源码、报告或归档。切换验证见 `docs/duplex-model-dsv41-20260917.md`。
- 2026-09-18 整题重跑使用 `private/models-dsv41-8192.json`（执行 max_tokens=8192），接口关闭思考参数不可靠。结果见 `docs/duplex-13-1-dsv41-rerun-20260918.md`。旧 `/tmp` 环境已丢失；最新可复用源码、SDK、原题和评分器在持久化 `office-code-20260915/rerun-13-1-dsv41-20260918-v3`，LibreOffice 在 `office-code-20260915/runtimes`，执行环境的 `soffice`/`libreoffice` 入口在 `native-venv/bin`。
