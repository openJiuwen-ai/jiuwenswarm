---
id: B_20261112_2110_explicit-default-flag-leak
title: 显式默认标记泄漏到模型详情与配置写入
type: B
author: LaiYongQiang
author_email: laiyongqiang1@huawei.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/model_catalog.py
  - jiuwenswarm/common/config.py
  - tests/unit_tests/test_model_resource_fixes.py
designs: []
design_note: MR !10012 的补充修复，依据既有 models.get / models.upsert 契约，无独立设计。
---

# 显式默认标记泄漏到模型详情与配置写入

## 现象与影响

MR !10012 引入运行时标记 `_explicit_default`，用于让操作员显式设置的默认模型优先于
按 model_name 分组推断出的默认。该标记由 `config._decrypt_model_entries` 写入内存条目，
但 `ModelCatalog.get_public_model_detail` 直接深拷贝 snapshot 条目返回，
`config._merge_model_update` 又按 key 合并写回。两者叠加后，前端「编辑模型」一次保存就会把
`_explicit_default` 持久化进用户 `config.yaml`，成为与运行时状态不一致的陈旧字段。

## 关联设计与当前契约

`models.get` 返回的是可编辑 DTO，`models.upsert` 以合并语义写回，未出现的字段保持不变。
`_merge_model_update` 已明确排除 `source`、`is_agentos`、`read_only`、`write_only_fields`
这类只存在于响应中的字段，`_explicit_default` 属于同一类运行时状态，应同样排除。

## 根因与修复决策

修复分两层，避免只堵一处：

- `ModelCatalog.get_public_model_detail` 在深拷贝后立即 `pop("_explicit_default")`，
  保证对外详情不含运行时标记。
- `config._merge_model_update` 在合并前同样 `pop`，并把该 key 加入跳过集合，
  使即使调用方回传旧响应也不会落盘。

不把标记改名成公开字段：前端不消费它，公开后反而扩大契约面。不在写入阶段重算：
`_decrypt_model_entries` 才是唯一计算点，重复计算会引入两个真相来源。

## 回归证据与适用范围

- 新增 `test_explicit_default_marker_never_reaches_detail_or_config`：先给条目注入标记，
  断言详情不含该键、upsert 返回值不含该键、写回后的配置条目也不含该键。
- 模型路由、Team 配置、模型选择、配方准备、迁移、运行时目录、模型资源与配置共八个测试文件，
  合计 453 passed、2 skipped。
- 命令：工作树 `.venv/bin/python -m pytest`，指定上述文件，使用 `-q -o addopts='' -p no:cacheprovider`。

## 防错模式与资料同步

运行时派生字段必须在对外投影处剥离，不能依赖下游调用方自觉不回传；
合并式写入应维护显式的字段跳过清单，新增内部字段时同步加入。

## 遗留与后续

CI 侧 UT 报告指向的失败用例 `test_choose_prefers_explicit_default_over_list_order`
已在 MR 当前 head 上通过，本记录不覆盖那次失败；其根因是提交遗漏了
`common/model_catalog.py` 的字段投影，已由 MR 内后续提交补上。
