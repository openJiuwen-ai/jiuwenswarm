# 复现与演示说明

## 本机复现（已验证范围）
1. 运行Run-Research-Demo.ps1。无需API Key，不下载文献、不发起模型请求。
2. 打开research/demo-latest.json所指目录，查看paper.pdf、figure-data.json、evidence/statistics.json及paper-inputs.json。
3. 比对固定计数36/35/27/32；记录review_basis=user_accepted_scoring，不当作独立双人评审。
4. 查看demo-verification.json的步骤、耗时和人工边界；最终PDF仍需作者审阅及外部Reviewer。

## 新机器恢复（材料已准备，未声称异机实测）
使用上游固定commit 49c9f5b4c66d193e4168bfa9c82c2df025c773f0，加发布包source.patch，再运行restore-source-bytes.py。也可使用记录的本地提交恢复源码。
参照锁定依赖安装Python环境，保留相同包版本；图表需要Matplotlib。准备本地Tectonic，首次补齐其公开排版依赖后，演示默认使用--only-cached。
旧私有v118证据包保存完整正式运行；追加当前formal-final的A/B/adjudications三份评分文件及intake-v120确认记录，再运行pipeline审计。仅有候选提交包中的源码和统计摘要不足以恢复原始运行；需受控移交完整实验记录。第三方全文不默认随公开PR发布。

## 不能混淆
离线重放验证软件与已有结果的衔接，不是再次执行384次模型调用，更不保证供应商模型能逐字重现。模型别名、未固定随机参数、共享前处理与输入截取均限制复现解释。任何付费重跑应另列授权、预算与新运行编号。
