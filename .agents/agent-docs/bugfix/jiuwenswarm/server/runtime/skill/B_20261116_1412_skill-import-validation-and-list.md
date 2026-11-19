---
id: B_20261116_1412_skill-import-validation-and-list
title: 技能导入元数据与 ZIP 路径安全校验修复
type: B
author: zhangzhongteng
author_email: zhangzhongteng1@huawei.com
modules:
  - jiuwenswarm/server/runtime/skill
files:
  - jiuwenswarm/server/runtime/skill/skill_manager.py
  - tests/unit_tests/agentserver/test_skills_import_upload_and_knowledge.py
  - tests/unit_tests/agentserver/test_skill_manager_path_safety.py
  - tests/unit_tests/agentserver/test_skill_manager_clawhub_owner_handle.py
  - tests/unit_tests/test_app_web_skill_upload.py
  - docs/zh/技能.md
  - docs/en/Skills.md
designs: []
design_note: 用户确认统一个人版技能导入和市场安装的元数据校验，并修复 ZIP 原始文件名及路径检查；无独立设计。
---

# 技能导入元数据与 ZIP 路径安全校验修复

## 现象与根因

导入缺少 `name` 的技能包时，展示解析器先使用文件名补为 `SKILL`，随后包校验将兜底名称当作有效声明，错误放行。单个 Markdown 文件导入还会把 YAML 数字名称转换为字符串，与目录、ZIP 的原始类型校验不一致。

本地来源校验允许 frontmatter 前有空行，但展示解析器和包名称校验使用另一套匹配规则，导致相同文件在不同阶段丢失元数据。远程归档允许缺少 `description`，ClawHub 和 SkillNet 也各自使用展示解析，安装入口之间没有统一的必填字段约束。

Python `zipfile` 会在 NUL 处截断 `filename`，仅检查处理后的名称无法识别原始非法输入。ZIP 原有检查还会跳过绝对路径成员，路径穿越检查则可能在其他成员已写入临时解压目录后才触发。

## 修复

`skill_manager.py` 的 `_parse_skill_frontmatter` 统一 frontmatter 匹配和 YAML 解析，允许前置空行，并区分缺少 frontmatter、无效 YAML 和非映射对象。`_validate_skill_metadata` 在展示兜底前检查原始字段：`name`、`description` 必须显式声明为去除首尾空白后非空的字符串；名称复用 `_validate_skill_name` 的现有规则。

本地单文件、目录、上传 ZIP、令牌导入、远程归档和个人版三个市场安装入口共用上述规则。`source_trusted` 仅保留跳过已完成来源检查的用途，不再豁免描述要求。元数据失败使用 `SKILL_INVALID_METADATA`，各入口保留原有响应结构。ClawHub 继续按市场 slug 登记，保持同名包的来源身份规则。

展示解析器复用 frontmatter 解析规则，保留文件名和旧格式兜底；安装校验不消费这些兜底字段。显式填写 `name: SKILL` 合法，源目录名也不必与技能名一致。已有技能的读取展示不增加安装阶段的严格校验。

ZIP 解压在写入首个成员前检查全部 `orig_filename`，拒绝 NUL、绝对路径、Windows 盘符路径和 `..` 路径穿越；保留原有大小、数量及深度限制。上传入口对这些不安全路径返回 `SKILL_UNSAFE_PATH`。

中英文技能指南同步必填字段、前置空行、多行描述和导入失败后的处理方式。旧远程包缺少描述时，需要补充后才能再次安装。

## 验证

使用 Python 项目虚拟环境，在源码根目录设置 `PYTHONPATH` 后执行：

```bash
python -m pytest -o addopts='' \
  tests/unit_tests/agentserver/test_skills_import_upload_and_knowledge.py \
  tests/unit_tests/agentserver/test_skill_manager_path_safety.py \
  tests/unit_tests/agentserver/test_skill_manager_clawhub_owner_handle.py \
  tests/unit_tests/agentserver/test_skill_manager_skill_display.py \
  tests/unit_tests/agentserver/test_skillnet_install_session.py \
  tests/unit_tests/agentserver/test_skill_manager_team_skills_hub.py \
  tests/unit_tests/agentserver/test_skills_import_local_token.py \
  tests/unit_tests/test_app_web_skill_upload.py \
  tests/unit_tests/agentserver/test_skill_sync.py \
  tests/unit_tests/agentserver/test_skillpack_backend.py \
  tests/unit_tests/agentserver/test_skillpack_inventory_guard_concurrency.py \
  tests/unit_tests/agentserver/test_skillpack_direct_route.py \
  tests/unit_tests/symphony/test_skillpack_tool.py \
  -q -rs --tb=short
```

结果为 **673 passed、11 skipped**，退出码 0。跳过项全部为 Windows 专属的敏感目录或长路径测试。

新增用例覆盖缺失、空白和类型错误的字段，非法名称、无效 YAML、前置空行、多行描述、显式同文件名名称，以及展示解析的旧文件名兜底。各导入入口拒绝无效元数据后，技能目录和状态文件保持不变；ZIP 不安全成员位于合法成员之后时，解压目录仍为空。

HTTP 用例启动真实 `ThreadingHTTPServer`，通过生产 `_SpaStaticHandler` 处理 `POST /file-api/skills/import`：合法包返回 200，技能文件和登记状态均已写入；缺失名称、数字名称、数字描述、NUL 和路径穿越返回 400，技能目录与状态保持不变。

市场和远程归档测试替换网络下载边界，未连接真实市场。未进行浏览器交互、Windows 实机或完整应用进程验证；未运行格式化或静态检查。
