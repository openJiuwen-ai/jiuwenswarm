---
id: B_20261111_1740_stop-task-title
title: 全双工停止结果使用任务显示名称
type: B
author: yzdnh123
author_email: yzdnh123@gmail.com
modules:
  - jiuwenswarm/extensions/video_duplex
files:
  - jiuwenswarm/extensions/video_duplex/frontend/taskDuplexJobs.ts
  - jiuwenswarm/channels/web/frontend/tests/taskDuplexLifecycle.test.mjs
designs: []
design_note: 恢复现有任务标题契约，无独立设计。
---

停止任务的结果曾直接使用 question，导致显示用户原话。现在按 title、question、query、默认名称的顺序选择非空名称。原始指令保持完整，不替换任务身份。

验证：运行 taskDuplexLifecycle.test.mjs，覆盖部分取消回执沿用已知显示名称、迟到完成不覆盖取消、停止记录只写入一次。全部通过。未进行浏览器人工验证。
