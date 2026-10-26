# 设计资产

本目录维护 JiuwenSwarm 的应用与跨仓接入设计。流程及模板见
[jw-design](../../skills/jw-design/SKILL.md)，实现与开发记忆见 [jw-dev](../../skills/jw-dev/SKILL.md)。

| 目录或文件 | 用途 |
|---|---|
| `proposals/` | 草稿、评审、批准待开发或否决的方案 |
| `active/` | 已批准且实际进入开发的方案 |
| `archive/` | 已交付、被取代、终止的设计及历史资料 |
| `INDEX.md` | 从标准 design.md 自动生成的模块与生命周期索引 |
| `REGISTRY.md` | 迁入设计的历史 SDD 编号，不分配新编号 |
| `legacy-documents.md` | 未补造 frontmatter 的旧格式文档入口 |
| `migration-20261026.md` | 本次迁移清单、维护归属与跨仓定位 |

新设计使用 `D-YYYYMMDD-HHMM-<slug>/design.md`，时间采用 Asia/Singapore、精确到分钟。
模块填写仓库相对路径，如 `[jiuwenswarm/agents, jiuwenswarm/server/runtime]`；跨仓模块使用完整的 `openjiuwen/agent_teams` 等路径。
迁入历史文档保留编号、审批、日期、状态与正文；索引中的历史模块名称不代表本仓拥有 core 实现。
标准设计见 [INDEX.md](INDEX.md)，非标准资料见 [legacy-documents.md](legacy-documents.md)。

从仓库根目录执行：

```bash
source .venv/bin/activate
export PYTHONPATH=.:$PYTHONPATH
python .agents/skills/jw-design/scripts/generate_index.py --root .agents/agent-docs/designs
```

工具依赖 PyYAML，复用仓库环境。`validate_frontmatter.py` 作为可选文档工具保留，在本地禁止静态检查的约束下不执行。
设计只记录开发前方案；当前契约维护在 `../specs/`，实施与决策归档在 `../features/`，故障证据在 `../bugfix/`。
转测文档由 jw-testing 输出到被 Git 忽略的 `testing/`，独立于设计生命周期且不进入设计索引。
core 底层设计继续在原仓库维护，位置见 [迁移说明](migration-20261026.md)，不要复制为 swarm 的当前契约。
