---
id: B_20261109_2218_team-startup-lock-windows
title: 同步 develop 后修复团队启动清理锁在 Windows 上的导入失败
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/enterprise/team
files:
  - jiuwenswarm/enterprise/team/session_cleanup.py
  - tests/unit_tests/enterprise/test_team_session_cleanup_lock.py
designs: []
design_note: 无独立新设计；保持 develop 的跨进程启动清理互斥语义，复用仓库已有跨平台锁依赖。
---

# 团队启动清理锁的 Windows 兼容

## 现象与版本

同步 develop `be95ee9903fccf2188ce90f248f696556a1a13bf` 时，Worker 执行宿主保留上游新增的团队启动清理任务。
Windows 复验出现 `ModuleNotFoundError: No module named 'fcntl'`，后台 Runtime 启动任务异常。
上游该版本的 `session_cleanup.py` 在模块顶层直接导入 fcntl；不能以此前的 IPC 监听或 readiness 回调替代完整准备链的验证。

## 根因与修复

fcntl 只在 POSIX 系统提供。启动清理模块会在普通 Agent 启动链中导入，异常并不局限于实际执行 MySQL 清理时。
将 `_sweep_lock` 改为使用仓库已依赖的 portalocker；延迟到真正获取锁时导入，保持原锁文件、独占等待和退出释放语义。
没有增加依赖、跳过互斥、修改用户数据或改动 OpenJiuwen SDK。

## 验证与边界

- 新增真实子进程用例：父进程持锁时子进程无法取得同一文件的独占锁，父进程释放后子进程可以取得。
- 新增异常退出用例：清理代码抛出异常后，另一进程仍能重新取得锁。
- 同步后定向回归 474 项通过，包括上述两项、Worker 生命周期、配置优先级、团队存储、PersonalContext、工作流恢复、历史和压缩。
- Source 真实浏览器冒烟通过配置就绪、发送请求、真实 SDK 调用本地模型协议桩和完整可见回复；测试拥有的 22 个进程全部回收。

环境为 Windows、Python 3.11.15、OpenJiuwen 提交 f3f8f576164cc405c046e43079c8ad00ee17f0c4、Gateway 提交 435d3323425e1051c6fe2d1feadcba68b4be020d、协议提交 4c5f3e9eb41bd33d34d012b06a3e364e8497ac11。
日志、JUnit 和浏览器证据位于仓库外 `D:/work_space/.codex-temp/startup-pr-delivery-20261110/`。
上述结果不覆盖真实 MySQL、多副本共享卷、Linux 系统测试或 Frozen/Electron 制品重建。
