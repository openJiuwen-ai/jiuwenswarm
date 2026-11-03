# 转测文档的独立生命周期

本文件只管理转测资料的评审与测试记录，不改变设计的 proposals / active / archive 阶段。
文档 `stage: testing` 表示其资料类型，不进入设计工具的阶段扫描或索引。

## 状态

| 状态 | 含义与证据 |
|---|---|
| draft | 正在撰写，测试准备或范围可能尚待确认 |
| in-review | 已交给实际评审者，记录真实 reviewers |
| changes-requested | 评审要求修订，记录反馈及处理情况 |
| in-testing | 转测资料已确认，实际开始测试，记录 approved-date |
| completed | 本次测试已完成且验收结论满足确认条件，填写 completed-date 和实际结果 |
| failed | 存在不满足验收要求的实际结果，记录失败证据和影响 |
| rejected | 本次转测资料或申请被明确否决，记录原因 |

通常沿 `draft → in-review ⇄ changes-requested → in-testing → completed / failed` 推进。
failed 修复后可回到 in-review；明确否决时进入 rejected。
completed / rejected 是本次转测的终态，新增范围需明确记录新一轮测试的版本与边界，保留旧结果。
每轮实质评审增加 version、更新 updated 和评审记录，不更改稳定文档标识。

## 元信息与结果边界

- 通用字段为 id、title、slug、status、stage、author、author_email、reviewers、created、updated、version、domain。
  负责人、评审者未知时明确待确认，不把模板示例当事实。
- `design-id` 指向实际关联设计，支持新 D- 标识和旧 SDD；无设计时说明实际 feature / spec 依据。
- `test-type` 按实际范围选 functional / integration / e2e / performance / security / regression。
- `implementation` 可记录实际目标代码、PR、commit 或版本，用于复现；不存在时不填造。
- 文档准备、资料评审、代码合并、开发自测、正式测试、用户验收分别表达；撰写完成不代表测试通过。
- 没有真实执行证据时结果为待验证，不能进入 completed；存在失败或阻塞时如实记录。
  有条件通过应列出具体条件和确认依据，条件未满足前不能标记 completed。
- 日期、用例统计、缺陷、测试范围和结论保持一致；原始日志或报告引用唯一证据入口。

转测文档暂存于被 Git 忽略的 designs/testing/；本文件不授权跟踪、提交或发布这些资料。
