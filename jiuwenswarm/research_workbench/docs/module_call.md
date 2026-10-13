# 接口与运行示例

## 一键本地演示
在桌面比赛文件夹中运行 `./Run-Research-Demo.ps1`。也可在项目虚拟环境下执行：

```powershell
python -X utf8 -m jiuwenswarm.research_workbench.native_verify --research "桌面项目/research" --demo
```

前提：Python依赖已安装；RESEARCH_WORKSPACE由脚本设为本项目；Tectonic及所需排版资源已经缓存；已有冻结实验和评分文件。无API Key也可运行。输出位置在`research/demo-latest.json`，每次产生独立`paper-native-*`目录。

## 原生工具
工具名`research_evidence`，仅接受一个action字段：

```json
{"action":"audit"}
```

- audit：读取本地固定证据、运行清单及用量日志；输出核验数与协议hash，不判断答案语义。
- prepare：在独立目录生成report/statistics/resource_report/writing-materials与清单，输出评分来源限制。
- draft：调用同一生成器，输出英文源码、图表、输入指纹路径；工具本身不编译、不调用模型。

其他action、任意路径、shell命令和额外字段都会被拒绝。`RESEARCH_WORKSPACE`必须是显式绝对路径。
程序入口为`native_research.ResearchEvidenceTool.invoke`，返回字典；异常为ValueError或底层文件/编译错误，不能自动改为成功。

## 离线与网页接口
`python -m jiuwenswarm.research_workbench.pipeline --research PATH`执行离线核验和统计。
`Build-Paper.ps1`生成新稿；`Build-Paper.ps1 -OutName paper-development-v5 -VerifyOnly`只验证指定稿件。
本地工作台GET `/api/pipeline/status`读取状态；POST `/api/pipeline/refresh`需要当前页面的X-Research-Token，重新核验并更新材料。该nonce不是模型API密钥。
旧付费阶段接口与离线演示分离；不要为了运行演示打开付费开关。

## 错误处理
| 情况 | 行为与处理 |
|---|---|
| 冻结代码/文献/hash不一致 | 停止，不重写freeze；按清单恢复正确字节，Windows换行用还原脚本处理 |
| 评分缺失或必需值不明确 | 保持待评，不用0冒充缺失；不得复制用户接受来伪造独立签名 |
| 用户接受的原评分后来变化 | 哈希不匹配，停止并要求重新确认对应版本 |
| 新稿目录已存在 | 拒绝覆盖，使用新目录 |
| 缺排版依赖 | 保留源码及build-console；先补齐公开依赖，随后重新验证离线编译 |
| PDF/源码/图表变化 | 指纹检查失败，不能沿用旧Reviewer凭据 |
| 原生工具异常 | finally卸载工具，保留日志，不触发模型重试 |
