# 墨研工作台 v0.5 + 全文数据工具
pilot-0.1 / pilot-0.2 / ablation-0.1已分别保存真实开发运行；累计80请求，32回答，仅6道不同开发题。报告不能把机械检查当语义支持或把开发修复当正式收益。
reporting.py离线审计产物、用量和评分绑定；fulltext.py离线校验PDF/页文本并构建可追溯的UTF-8分块。全文数据尚未正式实验。详情见桌面research/复现指南-v05.md与全文数据工具说明-v1.md。
启动：python -m jiuwenswarm.research_workbench.app。单进程，本机服务；预算持久化、Key仅会话内存、自动重试0。保存设置不执行模型。
软件测试覆盖7个test_*.py模块；不把测试数量当作科研实验数量。


## v0.7 离线研究衔接

`python -m jiuwenswarm.research_workbench.pipeline --research ../research` 验证固定PDF、五个冻结执行模块、运行产物与请求usage，再读取双人评分并生成论文材料。无模型调用。默认评分路径为`research/evaluations/formal-final/reviewer-A.json`、`reviewer-B.json`，可选`adjudications.json`。

工作台“任务与产物”中的“核验并更新材料”调用同一流程。先打开本地启动器；API只监听回环地址，所有写操作要求页面nonce。评分不全时effect=null，原运行不变。

`review_scoring --help`提供原v1.16 Markdown的严格导入，保留原评语及文件hash。两个不同人完整一致或有绑定双方hash的裁定才能统计；不确定不自动转0。固定分母与按来源组bootstrap用合成评分测试，合成数据不进入研究记录。

`python -m jiuwenswarm.research_workbench.submission PATH`仅检查本地候选包。缺最终评分、最终PDF/Reviewer绑定、团队名或贡献PR时退出2。不生成Reviewer结果，不提交文件。

正文方法、实验设置与资源可先写；最终效果、讨论与Reviewer评估仍等待真实评分。原生Team调度器的全流程运行未验证，不能仅因新增模块就声称全自动科研完成。
