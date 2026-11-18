---
id: B_20261118_1531_agent_group_install_timeout
title: 专家团安装请求提前超时
type: B
author: huanghongyun
author_email: huanghongyun1@huawei.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/runtime/agent_adapter/interface.py
  - jiuwenswarm/channels/web/frontend/src/features/agentManagement/groupClient.ts
  - tests/unit_tests/server/extensions/test_chat_send_extension_mount.py
designs: []
design_note: 修正现有专家团安装请求的超时顺序。
---

## 现象与根因

[Issue #6426](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6426) 的专家团安装走 Web 请求默认的 15 秒超时。前端到时移除待处理请求，服务端仍下载并安装，迟到的成功回执无法更新这次操作。

## 修复与验证

专家团安装在服务端使用 220 秒总超时，沿用既有安装超时响应。前端该请求等待 240 秒，留出返回响应的时间。单个专家、插件和通用请求继续使用各自的原有预算。

发布分支基线为 `3202398f9`。macOS Chrome 中经过带 18 秒额外详情请求延迟的 Hub，两个真实专家团并发安装分别耗时 30.57 和 29.88 秒，均成功落盘并出现在「我的专家」，期间未出现「请求超时」。故障复现使用同一延迟条件，原实现约 15 秒即报错。

扩展及市场后端测试 927 项通过、12 项跳过。专家管理及专家团管理前端测试合计 103 项通过。路由回归覆盖三个安装入口的预算和既有超时响应。原 Windows 每日构建包未重测。

`develop` 基线 `78c1fac71` 同步后，以相同慢链路条件验证两个专家团分别耗时 28.81 和 35.15 秒，均成功安装且界面未报超时。后端 925 项通过、14 项跳过，前端 104 项通过。
