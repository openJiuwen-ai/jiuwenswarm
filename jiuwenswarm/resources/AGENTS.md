---
description: resources 静态资源目录说明书——给修改/检视本目录内容的 AI 助手与工程师：出厂模板与配置基线的构成、个人版初始化 / 企业租户播种 / 技能目录 / 密钥注册表四类消费方式、模板与用户副本分离等修改纪律。
---

# resources

包内 **shipped 静态资源**：出厂配置模板、agent 工作区模板、内置技能模板与
配置基线（256 个文件：根级 8 件 + `agent/` 子树 248 件）。**不是代码模块**——
本目录内容在首次初始化或租户懒创建时拷贝到用户工作区，运行时读写的是用户侧
副本，不回写这里。

## 目录构成

| 路径 | 内容 |
|---|---|
| [config.yaml](config.yaml) | 出厂配置模板——用户 `config/config.yaml` 稀疏 override 的基线 |
| [.env.template](.env.template) | 环境变量模板（**仅个人版**消费；企业版由部署侧 envFrom 注入） |
| [builtin_rules.yaml](builtin_rules.yaml) | 内置规则基线，随 config 一起拷贝 |
| [secret_registry.yaml](secret_registry.yaml) | 敏感配置注册表 bundled 基线（约 60 条） |
| [secret_registry.enterprise.yaml](secret_registry.enterprise.yaml) | 企业版差异条目（单独维护） |
| [config.team.distributed.leader.yaml](config.team.distributed.leader.yaml) / [config.team.distributed.teammate.yaml](config.team.distributed.teammate.yaml) | 分布式 team 的 leader / teammate 配置模板 |
| [skills_state.json](skills_state.json) | 内置技能安装状态基线 |
| [agent/workspace/](agent/workspace/) | 工作区骨架模板：`AGENT/HEARTBEAT/IDENTITY/SOUL/USER/MEMORY.md`（`_ZH`/`_EN` 双语成对）+ `skills/`（20 个内置技能目录，含 SKILL.md / scripts / references） |

## 消费方式（谁在运行时读这里）

| 消费方 | 行为 |
|---|---|
| `common/utils.py: prepare_workspace`（:1456 起） | init / start 时把 config.yaml、builtin_rules.yaml、.env.template（仅个人版）、agent/ 模板树、skills_state.json 拷入用户工作区 `jiuwenclaw_workspace` |
| `common/utils.py: seed_tenant_agent_workspace`（:2210 起） | 企业租户工作区懒创建时按语言后缀（`_ZH`/`_EN`）播种六个 workspace md，**已存在不覆盖** |
| `common/utils.py: get_builtin_skills_dir`（:2638） | 内置技能目录解析：`agent/workspace/skills` 优先，回退旧布局 `agent/skills`（当前目录树只有前者） |
| `common/secrets/registry.py` | secret_registry 双层合并：bundled 为基线 + 用户 `config_dir` 同 key 覆盖（契约见 `common/docs/specs/S_04_secrets.md` N5） |
| `agents/harness/team/distributed_runtime` | leader / teammate 两件配置模板（`tests/unit_tests/test_resource_config.py` 直接校验） |
| `scripts/watch-folders.js` / `generate-agent-folders.js` | 前端开发脚本消费 `agent/` 模板 |

## 修改纪律

1. **模板与用户副本分离**：改本目录 = 改**出厂基线**，只影响新初始化的工作区；
   已部署用户的副本不会被自动刷新（`overwrite=True` 的 init 显式重置除外）。
   改模板字段时先想清楚存量用户拿到的是旧值。
2. **config.yaml 字段存在性驱动 override 清理**：用户 override 中"模板已不存在的
   字段"会在 `cleanup_override_against_template` 时被移除（utils.py:1450-1453）——
   **删模板字段 = 删用户的 override 字段**，改名等于丢配置。
3. **双语文件成对维护**：`AGENT/HEARTBEAT/IDENTITY/SOUL/MEMORY` 均有 `_ZH` /
   `_EN` 两份，落盘时按语言偏好选一并去后缀；只改一边 = 另一语言用户拿旧文案。
4. **新增敏感配置项必须登记 secret_registry.yaml**：storage / location / field
   三要素缺一不可（登记口径见 `common/AGENTS.md` 与 `common/docs/specs/
   S_04_secrets.md`）；企业差异写 `secret_registry.enterprise.yaml`，不并入基线。
5. **内置技能放标准位**：新技能目录放 `agent/workspace/skills/<name>/`
   （`SKILL.md` 必备），并同步 `skills_state.json` 的安装状态记录
   （`_update_skills_state_for_builtin`，utils.py:828 起）。
6. **`.env.template` 的企业版语义**：企业版不产生也不读取 `config/.env`
   （运行时守卫 `dotenv_early.load_dotenv_runtime`）——往这里加企业版才用的
   变量没有意义，企业侧走部署注入。

## 测试

- `tests/unit_tests/test_resource_config.py`：根级配置模板（含 team 分布式两件）
  的结构校验。
- 密钥注册表相关单测见 `common/`（`S_04_secrets.md` 的测试索引）。
- 读到不一致当场修文档：本文件里与现状不符的路径、文件名、点名数量的结构表述（如
  "三要素"、"双语成对"），先 ls / grep 核实、以现状为准刷新文档，同一次改动落地；
  不把过时表述当新约束执行。
