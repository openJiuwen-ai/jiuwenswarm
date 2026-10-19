# AgentGroup 资源包最小验收链路

本文用于批量转换专家团后，对未打包目录和最终 ZIP/TAR 资源包做可复现验收。验收脚本不实现第二套 manifest、归档或安装算法，只编排 JiuwenSwarm 的生产 loader 和正式 `agent_groups.*` 请求入口。

## 最小证据闭环

| 阶段 | 真实入口 | 通过条件 |
| --- | --- | --- |
| 目录契约 | `load_agent_group_package_bundle` | 根 manifest、成员 roster、persona、Skill、leader 约束均可由生产 loader 解析 |
| 导入与安装 | `JiuWenSwarm.process_message` + `agent_groups.import_local` | 生产归档解包、目录定位、校验、复制、marketplace 安装全部成功 |
| 消费与展示 | `agent_groups.list` + `agent_groups.show` | 新包显示为 `source=local`、`installed=true`、`canUse=true`，成员数与已安装 runtime 包一致 |
| 持久化 | `resolve_agent_group_dir` + 生产 loader 二次读取 | 实际安装目录存在，且复制后的包仍能被生产 loader 解析 |
| 源码 Web 消费 | develop Web 的“我的专家 → 专家团” | 页面显示已导入的 10 个专家团卡片，无缺失或重复，名称、头像和详情可见 |

脚本要求 `import-local` 使用空的 `JIUWENSWARM_DATA_DIR`，避免已有包、marketplace 状态或手工 seed 掩盖问题。一次进程可连续验收 10 个包，并用 `--expected-count 10` 防止漏包。

## 环境依赖

- Python `>=3.11,<3.14`。
- 仓库 `uv.lock` 中锁定的依赖，特别是与当前 JiuwenSwarm commit 配套的 `openjiuwen`。
- 标准环境执行 `uv sync`。该验收不需要模型 API Key，也不需要连接 Hub。
- 当前 macOS 12 x86_64 开发机存在独立环境限制：lock 中的 `faiss-cpu==1.14.3` 没有该平台 wheel；这不是 AgentGroup 资源包失败。Linux、Windows 或受支持的 macOS 构建环境仍应按 `uv sync` 建立标准 venv。

## 1. 校验转换后的未打包目录

```bash
uv run python scripts/acceptance/agent_group_package_acceptance.py \
  validate-only \
  --expected-count 10 \
  --json-out /tmp/workswarm-agent-groups-validate.json \
  /absolute/path/to/team-01 \
  /absolute/path/to/team-02
```

`validate-only` 只接受未打包目录。它无安装副作用，适合在打 ZIP 前尽快暴露成员、persona 或 Skill 引用错误。

## 2. 对最终归档执行黑盒导入 smoke

先准备一个不存在或为空的绝对目录，再传入最终交付 ZIP/TAR：

```bash
uv run python scripts/acceptance/agent_group_package_acceptance.py \
  import-local \
  --data-dir /tmp/workswarm-agent-groups-clean-home \
  --expected-count 10 \
  --json-out /tmp/workswarm-agent-groups-import.json \
  /absolute/path/to/team-01.zip \
  /absolute/path/to/team-02.zip
```

退出码为 `0` 且报告顶层 `passed=true` 才算通过。逐包至少核对：

- `packageId` 与 manifest 的 ID 一致；
- `show.source=list.source=local`；
- `show.installed=list.installed=true`；
- `show.canUse=list.canUse=true`；
- `memberCount` 与已安装 runtime 包一致；
- 每个 case 的 `show` 与 `list` 均包含团队/成员头像存在性、成员数和共享 `skillIds` 证据；
- `memberSkillIds` 记录生产 loader 实际装载到各成员的 Skill ID；
- `catalog.missingImportedIds` 为空。

头像还需执行一条打包前门禁：manifest 引用的团队和成员栅格头像必须保持原始比例、最长边不超过 `256px`，且格式为 WebP；WorkSwarm 原生支持的 SVG 可原样保留。原始高清栅格素材可留在包内，但不能被 manifest 作为运行时头像引用。`agent_groups.list` 会将引用图片编码为 data URL，因此仅检查“头像存在”不足以防止过大的 catalog 响应载荷。

若在正式 loader 启动前就发生路径不存在、隔离数据目录非空等 setup 失败，报告仍保留用户原始提交的 `packageCount` 和绝对 `packagePaths`，便于区分“没有提交包”和“已提交但 setup 失败”。

测试 fixture 回归入口：

```bash
uv run python -m pytest \
  tests/unit_tests/server/extensions/test_agent_group_package_acceptance.py -q
```

## 3. 启动 develop Web 环境人工复核

smoke 已覆盖 AgentServer 正式请求门面和用户可见 catalog 数据，但不替代浏览器文件选择器、前端上传交互或桌面安装包验证。需要页面复核时，使用同一个隔离数据目录：

```bash
export JIUWENSWARM_DATA_DIR=/tmp/workswarm-agent-groups-clean-home
make init
make start-debug-rebuild
```

服务启动后打开 `http://localhost:5173`，必须按真实用户路径进入“我的专家 → 专家团”，并确认页面能显示本次导入的 10 个卡片，无缺失、重复或明显卡顿；卡片名称和头像应正常，且可进入详情查看成员。这一步是用户可见的消费方 smoke，不能只用 `list` / `show` 接口响应替代。若前端提供本地导入入口，也应再通过页面导入一份新 ID 的包，证明文件选择器到 AgentServer 的网关链路正常。

**源码 develop Web 通过只证明源码进程中的前端消费链路可用，不等同于 Windows 客户端安装包通过。** Windows 桌面壳、文件选择器、打包 runtime、权限和文件保存行为仍需在目标安装包上单独 smoke。

## 验收边界

本链路已经验证源码 develop 环境中的生产 loader、AgentServer facade、安装持久化和 catalog 展示数据。它没有启动 Gateway/Web 进程，也没有覆盖 Windows 客户端壳、目标平台文件选择器和正式安装包；这些仍是发布前人工或目标平台 UI automation 的保留项，不能用本 smoke 冒充通过。
