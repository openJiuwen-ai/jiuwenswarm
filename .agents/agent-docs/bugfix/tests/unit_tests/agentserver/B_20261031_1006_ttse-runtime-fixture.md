---
id: B_20261031_1006_ttse-runtime-fixture
title: TTSE runtime UT 使用版本化夹具修复整批 setup error
type: B
author: xinyang_bali
author_email: 835397263@qq.com
designs: []
design_note: 修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。
modules:
  - tests/unit_tests/agentserver
  - tests/unit_tests/server/extensions
  - tests/fixtures/ttse_plugin
  - scripts
files:
  - tests/unit_tests/agentserver/test_ttse_plugin_runtime.py
  - tests/unit_tests/server/extensions/test_ttse_plugin_package.py
  - tests/unit_tests/conftest.py
  - tests/fixtures/ttse_plugin/manifest.json
  - tests/fixtures/ttse_plugin/config.yaml
  - tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py
  - scripts/package_ttse_plugin.py
---

# TTSE runtime UT 自包含夹具

## 现象与根因

CI 中 test_ttse_plugin_runtime 的共享 environment fixture 在 package_ttse_plugin 读取 manifest.json 时失败，导致多数用例为 setup error。当前仓库不跟踪且本地也不存在 jiuwenswarm/resources/plugins/ttse/manifest.json；测试却把该本地生产资源目录当成必需的版本化测试输入。

本地修复前用单个真实失败用例复现 FileNotFoundError。此前 B_20261031_0946_ttse-codecheck-boundaries 使用已安装包临时补源，只用于验证当时的代码修复，并未解决干净检出的测试问题。本次将该限制落地修复。

## 修复决策

- 增加版本化 tests/fixtures/ttse_plugin，包含 test-only manifest/config/README 和继承真实 core TTSERail 的代表性工厂。保留配置校验、旧业务选项兼容、服务注入、资源登记及关闭行为，继续通过宿主装备入口验证，不把 TTSE 算法复制到夹具。
- 根 conftest 提供 ttse_plugin_source，每个测试复制到自身 tmp_path。修改包版本/工厂等场景只编辑副本，兼容 xdist，不影响夹具源或用户数据。
- package_ttse_plugin 新增 keyword-only source 参数，CLI 新增 --source；不传时仍使用原生产目录。缺失 manifest 时给出明确的源目录与 --source 提示，不创建假制品、不回退到用户安装目录或测试夹具。
- runtime UT 与 package UT 显式传入版本化夹具目录，仍生成、解包并通过真实 core loader 与 import/install 边界验证。
- 新增负向检查：默认源缺失明确失败且不生成 ZIP；声明的 Rail 文件缺失也不生成 ZIP。可复现 ZIP 测试将默认源指向不存在目录，证明显式夹具输入独立于本地生产资源。

未采用 pytest.skip、捕获后吞掉 FileNotFoundError、重新跟踪用户排除的生产目录或读取 ~/.jiuwenswarm 已安装插件。测试覆盖定位为宿主运行契约；未跟踪的生产插件仍需在其发布流程独立验证，夹具不冒充发布源码。

## 验证

目标集：tests/unit_tests/agentserver/test_ttse_plugin_runtime.py 与 tests/unit_tests/server/extensions/test_ttse_plugin_package.py。

- 修复前选取 test_selection_without_old_switch_mounts_context_rail_and_unloads_cleanly：**1 setup error**，缺失 manifest.json。
- 修复后现有 .venv、PYTHONUTF8=1、PYTHONPATH=本仓;../agent-core，直接 pytest：**52 passed**，不再使用临时重定向打包器的脚本。
- 再以 PYTHONPATH=本仓;.git/ttse-ut-verify-deps、虚拟环境已安装 core 运行 pytest -n 2：**52 passed**。pytest-xdist 3.8.0 与 execnet 2.1.2 仅安装到隔离目录，未改依赖声明、uv.lock 或 .venv。此轮不从本地 ../agent-core 取包。
- 两轮均使用 -o addopts='' -o log_cli=false -o asyncio_mode=auto -q --tb=short --show-capture=no。本地为 Windows/Python 3.13；用户截图中的 Linux/Python 3.11 环境仍须由 CI 复跑确认。
- 实际检查生产 manifest 仍不存在，四个测试资源文件未被 .gitignore 排除。未修改用户 .gitignore、主 config、pyproject 或 uv.lock，未恢复生产资源目录，未触碰已安装插件或重启服务。

## 交接

提交修复时须包含 tests/fixtures/ttse_plugin 的四个文件、conftest、两个测试文件及打包函数变更。只提交测试引用而遗漏夹具仍会造成干净检出失败。未运行格式化或静态检查，未调用真实外部模型，未自动提交或推送。
