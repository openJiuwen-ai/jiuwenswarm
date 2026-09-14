# 专家池扩展：将专家团成员转为单专家

`scripts/flatten_expert_groups.py` 把既有 `agent_group` 的非主理人成员包装成可独立安装的
`agent_template`。它只读取源包并写入新的目标目录，不修改原专家团，也不会覆盖已有专家 ID。

已审阅配置在 `expert_pool_catalog.json`。当前优先覆盖：

- `humanize-ppt-team`：6 个 PPT 生产角色；
- `huashu-data-pro`：3 个数据分析角色；
- `software-company`：4 个软件研发角色。

未写入 catalog 的安全专家团成员在存在团队共享 Skills 时也可以转换：它们继承团队标签和
共享 Skills，并获得一句话 Quick Prompt 与最小协作元数据。没有任何真实 Skill 的成员默认
跳过；确有特殊需求时必须在已审阅 catalog 中明确写入 `allowNoSkills: true`。后续只需在
catalog 中补充更准确的能力、Skills 与产物契约，不需要修改 CLI。

## 使用

先把来源包放到一个隔离目录，使该目录的直接子目录分别是一个 `agent_group` 包。不要把包含
多个包的 `experts_cache.zip` 整体当成单个专家上传。

```bash
python scripts/flatten_expert_groups.py \
  --source-root /path/to/safe-agent-groups \
  --skill-root /path/to/reviewed-expert-package/skills \
  --skill-root /path/to/installed-skills \
  --destination-root /path/to/generated-standalone-experts \
  --catalog examples/xiaoyi_expert_graph_demo/expert_pool_catalog.json
```

CLI 以 JSON 输出 `created`、`skipped`、`warnings` 和统计。生成包中的 Skill 目录名来自
`SKILL.md` frontmatter 的原始 `name`，不会使用来源缓存目录的版本化别名。
`--skill-root` 可以重复；前面的目录优先，建议把已经人工审阅且目录名与 frontmatter `name`
一致的 Skills 放在通用安装缓存之前。

## 安全边界

- `academic-journal-selector` 固定隔离；
- 不复制符号链接、`.env`、`settings.json`、credential、secret、API key、token 或 password
  等敏感配置文件；
- 目标 ID 使用 `<来源团队>-<成员>` 命名空间，目标已存在时直接跳过；
- catalog 明确声明的 Skill 找不到时跳过该成员，避免生成“声称会调用但实际未挂载”的专家；
- 最终没有可用 Skill 的成员以 `no-available-skills` 跳过，除非 catalog 明确允许；
- 该工具不下载依赖、不调用外部服务，也不把来源附件或凭据写入 repo。

生成后应再走小艺 Work 的正式安装入口，并在全新会话中验证 Quick Prompt 和主产物。CLI 的
结构 smoke 不能替代真实前端与模型执行验证。
