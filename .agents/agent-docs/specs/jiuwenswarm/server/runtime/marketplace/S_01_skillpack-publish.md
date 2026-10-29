---
id: S_01_skillpack-publish
title: SkillPack 发布规约
type: S
author: 霆龙(吴建宇)
author_email: wujianyu@xiaohongshu.com
modules:
- jiuwenswarm/server/runtime/marketplace
- jiuwenswarm/channels/web/frontend/src/components/AssetPublishDrawer
files:
- jiuwenswarm/server/runtime/marketplace/skillpack_publish_service.py
- jiuwenswarm/server/runtime/marketplace/skillpack_publish_hub.py
- jiuwenswarm/server/runtime/marketplace/asset_publish_adapters.py
- jiuwenswarm/server/runtime/marketplace/asset_publish_models.py
- jiuwenswarm/channels/web/frontend/src/components/AssetPublishDrawer/index.tsx
designs:
- symphony-design-docs:SDD-0013
design_note: 依据 symphony-design-docs 仓库 proposals/SDD-0013-jiuwenswarm-skill-pack-productization/design.md（v9）；设计仓库：https://gitcode.com/lin-xiaoyu/symphony-design-docs。
---

# SkillPack 发布规约

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 关联模块 | `jiuwenswarm/server/runtime/marketplace` |
| 最近修订 | 2026-10-26 |
| 设计依据 | SDD-0013 v9 §4.2.3、§4.3.2–§4.3.3 |
| 实施记录 | [成员优先发布实现](../../../../../features/jiuwenswarm/server/runtime/marketplace/F_20261026_2035_skillpack-publish.md) |

## 范围与启用条件

SkillPack 继续使用 `assets.publish.describe/prepare/commit/status/records`，请求资源类型为 `skillpack`。普通 Skill adapter 拒绝根 `kind: skillpack`，避免绕过成员计划。

发布需要 `SKILLPACK_PRODUCTIZATION_ENABLED=true`，并显式配置 `SKILLPACK_HUB_DEPLOYMENT_SCOPE=isolated` 或 `version_gated`。此配置是部署者对测试市场或客户端版本门禁的声明；WorkSwarm 不会替 Hub 实现市场过滤。没有声明时禁止发布。

`SKILLPACK_HUB_TRANSPORT_PROFILE` 默认 `legacy_skill_v1`，也接受 `native_skillpack_v1`。前者要求 Hub 返回 `plugin_type=skill`，后者要求返回 `skillpack`。网络异常不得切换 profile；同一资产后续版本必须沿用首次发布配置。原生 Hub 是否已经实现由部署者核验。

## 发布不变量

1. Pack 和成员都来自服务端解析的当前工作区资源 ID；前端不能传入文件路径、Hub 地址、scope 或发布者 ID。
2. 成员必须通过 `resolve_skillpack_member_type` 的普通 Skill 判定。本期单次计划最多 32 个成员。
3. `prepare` 只在本地创建私有快照和计划，不上传、不改动已安装目录。全部阻断项解除后用户才能确认。
4. 用户确认绑定根文档、成员内容指纹、公开范围、名称和版本。成员均公开且审核通过后，才生成 lock 和上传 Pack。
5. Pack ZIP 只有既有 `plugin.yaml` 外层、根 `SKILL.md` 与 `skillpack.lock.json`。成员源码留在独立 Skill 资产；lock 复用 `skill/skillpack_lock.py` schema。
6. 成员身份为同一 Hub 的 `asset_id + version + artifact_sha256`。已有安装成员只有在来源记录、当前内容指纹和 Hub 精确版本摘要一致时可复用。普通发布历史只能通过同一账号的可信记录和当前制品摘要建立绑定。
7. 摘要取 `is_cli_download=true` 对应的原始上传 ZIP；公开资格通过无认证的精确版本详情与 artifact 访问再次验证。用户凭据只用于本人命名空间和审核状态查询。

## 接口和状态

`prepare` 返回 `members`，逐成员包含名称、版本、`reuse/publish/blocked`、公开范围及错误；返回的 `draft_id` 以 `skillpack-` 开头。它保留现有发布抽屉的 `can_submit`、`errors` 等字段。

`commit` 确认本地计划，返回持久化任务 ID。重复确认同一 draft 不重复上传。`status` 返回 `stage` 和逐成员进度，沿用现有 `execution_status`：进行中为 `uploading`，成功为 `completed`，阻断为 `failed`。

主要阶段为：

```text
prepared → publishing_members → waiting_member_review
         → publishing_pack → waiting_pack_review → completed
```

Pack 或成员变更、类型/身份冲突、审核驳回会进入 `blocked`。网络读取失败保留原阶段；认证失效进入 `reauth_required`；无法确认某次上传是否成功进入 `reconcile_upload`，禁止直接重发。

记录保存本地 `skillpack` 类型、Hub 类型、Hub 来源、资产 ID、版本、根 ZIP 摘要。当前 Hub 分别审核每个 Skill，不据此声称 Hub 已完成成员依赖闭包审核。

## 恢复与失败

进度保存在发布状态目录下 `skillpacks/skillpack-publish.sqlite`，普通成员/Pack 的单次上传继续复用 `publish.sqlite`。账号 scope 由服务端校验的 OAuth 发布者 ID、provider、gateway 用户、工作区和 Hub 共同派生；token 不落盘，刷新 token 不改变该 scope。

关闭抽屉后，存活进程继续轮询。进程重启后，用户重新打开记录并查询状态时携带当前 OAuth 恢复推进；不持久化 token，也不在无人授权登录的情况下自动读取私有审核结果。

结果未知的上传先按本人资产名、精确版本和原始摘要查询，匹配才接受既有结果，不能重发。明确未上传的进程中断或明确限流等失败，在同一快照和远端查询无匹配的前提下最多重试三次；每次仍使用持久化子任务身份。已成功的成员不重复上传，也不自动删除。

等待审核时内容发生变化，任务停止。若 Pack 尚未上传，可重新检查并确认一份新计划，旧的成员发布结果继续留在记录里。已经发布的成员内容改变时需要新成员版本；已发布 Pack 版本不可原地覆盖。

## 验证入口

- `tests/unit_tests/server/marketplace/test_skillpack_publish.py`：成员优先、一次确认、锁定 ZIP、身份限制、恢复和内容变化。
- 既有 `test_asset_publish_*`、`test_asset_package_builder.py`、`test_hub_publish_client.py`：普通发布回归和上传契约。
- 前端 `tests/assetPublishDrawer.test.mjs`：同一发布抽屉的成员计划、公开范围和审核阶段。
