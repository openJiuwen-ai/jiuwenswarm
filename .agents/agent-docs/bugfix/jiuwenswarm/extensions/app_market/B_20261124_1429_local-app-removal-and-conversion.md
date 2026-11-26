---
id: B_20261124_1429_local-app-removal-and-conversion
title: 应用市场本地清理和 Hub 转换的一致性
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/extensions/app_market
files:
  - jiuwenswarm/extensions/app_market/extension.py
  - jiuwenswarm/extensions/app_market/manager.py
  - jiuwenswarm/extensions/app_market/hub_install.py
  - jiuwenswarm/extensions/app_market/app_market/storage.py
  - jiuwenswarm/extensions/app_market/app_market/models.py
  - jiuwenswarm/extensions/app_market/app_market/market_service.py
  - tests/unit_tests/test_app_market_hub_install.py
designs: []
design_note: 本次修正应用市场登记、目录归属与转换行为，沿用 S_03 契约和现有项目接口，无独立设计。
---

# 应用市场本地清理和 Hub 转换的一致性

## 现象与根因

- 本地清理把安装根本身视为应用目录，并允许仅凭发布标记文件存在就清理安装根外的目录。登记删除错误被吞掉时，程序文件仍被删除。
- 转换先写本地清单，再等待 Core 回写绑定。本地删除未使用同一把锁，转换可以重建已移除的登记。新项目写入后，存活的旧实例仍使用原项目环境变量。项目目录一致时，转换也会清掉原编辑会话关联。

## 修复决策

- 程序清理以登记确认移除为前提，保留安装根、归档目录以及其他登记使用的目录。转换时从可信安装记录保留发布 ID，用于核验自选安装目录的标记。旧本地登记可继续加载，根外目录缺少归属凭据时保留并返回清理提示。
- 转换与删除使用同一应用锁。绑定变化时先停止旧实例，再写入转换后的清单。原项目与应用目录一致时继承项目及编辑会话绑定。
- 仅检查发布标记是否存在无法证明它属于当前登记。仅删除会话字段也会使原本有效的编辑预览关联消失，因此两处判断都依据资源归属。

## 验证

Windows 项目 `.venv` 定向验证 22 项通过。覆盖登记删除失败、安装根和重叠目录保护、伪造标记、转换后清理自选目录、会话保留、旧实例停止和并发删除。同步验证了相关的 Hub 更新、卸载、恢复及本地发布路径。Core 和运行实例生命周期使用测试替身，文件和清单在临时目录实际写入及核对。

当前契约见 [S_03 应用分发与安装](../../../../specs/jiuwenswarm/extensions/app_market/S_03_app-hub-distribution.md)，实施演进见 [Hub 应用分发 feature](../../../../features/F_20261119_1039_app-hub.md)。
