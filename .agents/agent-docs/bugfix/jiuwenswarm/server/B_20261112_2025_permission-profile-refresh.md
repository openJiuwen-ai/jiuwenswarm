---
id: B_20261112_2025_permission-profile-refresh
title: 刷新后智能审批档位错误回显为默认权限
type: B
author: nypdmax
author_email: chengkunck@gmail.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/server/control
files:
  - jiuwenswarm/common/permission_mode.py
  - jiuwenswarm/common/config_panel/config_set_handlers.py
  - jiuwenswarm/server/control/config_service.py
  - tests/unit_tests/agentserver/test_front_permission_profile.py
designs: []
design_note: 补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。
---

# 刷新后智能审批档位错误回显为默认权限

## 现象与影响

个人版选择智能审批后，刷新页面，输入框显示默认权限，执行仍按智能审批。
当前分支的个人版 Gateway 会把配置查询转发到 AgentServer，CONFIG_GET 由 Front
控制面处理。该查询路径缺少档位字段，因此单独刷新就足以触发，不依赖任务结束。

三个分支都有 Front 序列化遗漏。releases/0.2.8 的普通本地 Gateway 读取路径已返回
档位，只有经 AgentServer Front 的查询路径受影响；develop 与 releases/0.2.9
个人版正常查询路径也会经过 Front。

## 根因与修复决策

保存智能审批时，配置为 permissions.enabled=true、permissions.mode=auto，保存
响应携带 permissions_profile=automatic。Front get_panel 只返回旧的
permissions_enabled=true，App.tsx 在缺少 automatic 档位字段时回显 default。

把既有 permission_profile 函数从 common.config_panel.config_set_handlers 原样
迁到轻量 common.permission_mode，原模块导入并继续暴露该符号，保存端与查询端共用
一份映射。Front 正常查询补齐 permissions_profile，异常回退补齐 full_access。
没有把依赖 Runtime/OpenJiuwen 的配置写入模块引入 Front。

本次恢复既有 Web 消费契约，不增加审批策略、配置项或数据迁移。无独立模块 spec，
当前契约依据是 App.tsx 的权限回显分支、既有 canonical_permission_facade 与配置
读写回归测试；此记录保存本次故障与验证事实，不替代历史团队权限设计。

## 回归证据与适用范围

- 基线：e1e73174208e4fe1593d1ed8a395c319e3a4d9fd；修复分支：fix/permission-profile-refresh-develop，未提交工作区。
- 新增测试在三个未修复基线上均为 10 项失败，原因均为缺少 permissions_profile。
- 修复后新增 10 项通过：真实 YAML 文件经 handle_config_request(CONFIG_GET) 重复
  查询，覆盖智能审批、默认权限、完全访问、旧配置缺省、模式规范化、未知模式与读取
  异常；独立核对文件内容未变、运行时智能审批判定与档位一致。
- 既有 test_front_module_import_does_not_load_runtime 通过，保持 Front 轻量导入边界。
- 既有 Gateway/配置消费者测试 11 项通过，覆盖 canonical 权限读取、保存、
  save_all、写入失败和无效组合载荷；原导入入口保持可用。
- 环境：已有 Python 3.12.13 开发环境，经 uv run --no-project 执行；前次通过结果使用协议
  cbbadde9101db6e3d75e4ef2b9bdb6029fd92088 测试覆盖层，而本分支 pyproject.toml 与
  uv.lock 实际锁定 4c5f3e9eb41bd33d34d012b06a3e364e8497ac11。前次测试不能视为
  本分支锁定协议环境验证；独立检视阶段尝试真实锁定版本时，依赖获取失败，未进入
  pytest；直接 Git 拉取也遇到 SSL 连接错误，不能据此认定该提交不存在。
  Gateway 源码使用分支锁定
  54e5330d3ea6be972aa38de4c42a898ea73fed37，未修改依赖清单或锁文件。
- 现有开发环境的 openJiuwen 为 4d11bb88977f736504e56ed041c9961bfaa9f99b，
  本分支锁定为 f3f8f576164cc405c046e43079c8ad00ee17f0c4；以上结果不代表完整
  锁定依赖环境或安装包验证。
- 测试范围：新增 test_front_permission_profile.py；test_front_phase1.py 中 Front
  导入边界用例；test_app_web_handlers.py 和 test_config.py 中
  permission_profile/canonical_permission 相关用例。日志与 JUnit 结果位于本次任务的
  approval-refresh-20261112/validation/develop 隔离目录。
- 未执行 wheel 安装、真实浏览器或真实模型 E2E；未运行格式化和静态检查。

## 检视后补充的异常路径回归

独立检视指出真实 YAML 解析失败与权限投影后其他字段异常缺少专项覆盖。本轮新增
4 个用例，经 handle_config_request(CONFIG_GET) 真实入口验证：

- 从磁盘读取语法损坏的 YAML，确认捕获的异常为 YAMLError，响应为
  permissions_profile=full_access、permissions_enabled=false，原始文件不被改写。
- 从合法 YAML 读取权限后，让后续 react.evolution 的列表值触发 AttributeError；
  参数化覆盖 automatic/default/full_access，确认已读取的档位与开关被保留，
  文件内容及基于持久化权限的运行时智能审批判定不变。

相关回归共 29 项通过：新增权限文件 14 项、Front 轻量导入 1 项、已有配置读写消费者
及三档实际持久化 14 项。pytest 的选择条件为 permission_profile、permissions_profile、
canonical_permission 或 front_module_import_does_not_load_runtime；未更改生产代码。
本轮日志和 JUnit 位于 approval-refresh-20261112/followup-regressions/develop/。
本轮仍复用已有 Python 3.12.13 开发环境及 cbbadde9101db6e3d75e4ef2b9bdb6029fd92088
协议覆盖层，不是 develop 的实际锁定协议环境。
本轮再次尝试以实际锁定的 4c5f3e9eb41bd33d34d012b06a3e364e8497ac11 执行同一范围，
uv 仍未能获取并解析该依赖提交，未进入 pytest；日志保存于本轮 develop/pinned-pytest.log。
该失败仅作为依赖获取限制记录，不推断上游提交不存在，也不更改锁文件。

## 遗留与重验条件

### PR review 反馈修正

本次新增档位字段与 Front 原有宽松布尔显示规则组合后，非布尔 enabled（如字符串
"yes"、"true"、"1"、"on" 或整数 1）可能返回 full_access/true 的矛盾响应。
该问题由 !10109 的当前版本行评论指出；相同实现也存在于另两个修复分支。
修正将既有 canonical_permission_facade 原样移入轻量 permission_mode 模块，由
Front 和配置保存端共用，旧模块继续导出该符号。运行时 enabled is True 的判定不变。

新增 8 组非布尔与边界取值、1 组后续字段异常用例；修复前每分支 6 失败、17 通过，
修复后权限专项每分支 23 项全部通过。组合回归 38 项通过。
真实 YAML 测试继续验证 YAMLError，但不再要求整个查询过程只记录一次 warning，
避免把无关配置字段的后续日志绑定到该用例。原有 mock 读取失败及后续字段异常测试
已经覆盖 get_panel 的外层回退，无需再增加重复的异常测试。
环境和锁定依赖限制沿用前述记录；本轮原始反馈、红绿测试日志和 JUnit 位于
approval-refresh-20261112/pr-feedback-6541/，未运行格式化或静态检查。

PR 发布前重放到上游基线 df52a5c3d2a1410944fd30c85e7ae2ca1c7741ed，导入冲突仅合并保留上游 CUA 新接口与权限投影。
重放后相同 29 项回归全部通过，协议仍为上述 cbbadde 覆盖层；锁定协议验证与完整环境验证限制不变。
本轮发布验证产物位于 approval-refresh-20261112/publication-6541/develop/。

无已知范围内未解决缺陷。配置档位映射、Front 路由、Web 消费条件或分支依赖变化时，
重新执行上述回归；真实锁定协议依赖可获取后补跑相同相关用例，安装包端到端体验
仍需用相应构建验证。

