---
name: openJiuwen-DeepSearch
description: 知识增强型深度检索与深度研究，支持查询规划、信息收集、理解反思、研究报告生成等多 Agent 协同。使用场景：金融分析研报、学术与政策研究、企业级深度搜索等复杂推理任务，可以生成Markdown、Doc和Html格式的研究报告。**每次使用该技能之前都先完整阅读一遍SKILL.md学习技能。**


---

# openJiuwen-DeepSearch 技能使用指南

1. 每次使用该技能之前都先**完整阅读一遍SKILL.md学习技能**
2. **切换到 openJiuwen-DeepSearch 技能文件夹路径**，后续环境准备、配置和运行命令都必须在该目录下执行。
3. 每次运行前检查技能目录中是否存在可用的 `.venv` 虚拟环境。首次运行或 `.venv` 不存在时，必须先按照“首次运行准备”创建 Python 3.11 虚拟环境并安装依赖；环境准备完成前不得运行研究任务。
4. 每次运行前检查技能目录中是否存在 `.env`，并确认必需配置已填写且不是示例占位值。如果 `.env` 不存在、必需参数为空或仍为占位值，立即停止执行并提示用户在指定路径下先自行创建或编辑 `.env`；不得代替用户填写配置、不得要求用户在对话中提供 API Key，也不得运行研究任务。
5. 环境和配置检查通过后，运行 `uv run "scripts\main.py" --mode query --query "研究报告标题"`。已有可用 `.venv` 时直接复用，不需要执行 `uv sync`。
6. 执行命令后会启动子进程在后台执行，请确保**后台子进程正常运行**，你需要给出**openJiuwen-DeepSearch技能文件夹绝对路径**作为报告输出目录并提示用户等待约15分钟直至报告文件输出。
7. 技能执行时间约15分钟（使用非思考模式模型生成报告时），执行完上述命令后，该程序会拉起一个后台子进程完成报告生成任务，并且在openJiuwen-DeepSearch技能文件夹绝对路径下的PID.info中会输出该子进程的PID。你必须确保后台子进程正常运行并**读取PID.info中的PID**，之后直接结束当前轮次对话并提示用户等待约15分钟。当用户询问报告是否完成生成时，你需要**通过对应子进程PID的进程任务和openJiuwen-DeepSearch技能文件夹路径中的Markdown/Doc/Html文件列表判断是否完成本次研究报告的生成**。
8. 当用户要求停止当前研究任务时，立即按照”停止当前研究任务”由主 Agent 执行进程树终止和验证；只有确认根进程及其子进程均已退出后，才能告诉用户任务已停止。

## 首次运行准备

### 1. 准备虚拟环境

先确认已安装 `uv`；未安装时执行：

```bash
pip install uv
```

在技能文件夹根目录创建 Python 3.11 虚拟环境并安装固定版本依赖：

```bash
uv venv --python 3.11
uv pip install openjiuwen-deepsearch==0.1.8 python-dotenv pypandoc markdown markdown_mermaid_cli -i https://pypi.tuna.tsinghua.edu.cn/simple --prerelease=allow
```

### 2. 提示用户准备配置文件

`.env` 必须由用户自行创建并编辑。文件不存在时，提示用户在技能文件夹根目录从示例文件复制：

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

```bash
# Linux/macOS
cp .env.example .env
```

提示用户在本地编辑 `.env`，至少正确填写以下必需配置：

```env
LLM_MODEL_NAME=gpt-4o
LLM_MODEL_TYPE=openai
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=实际的_LLM_API_Key
WEB_SEARCH_ENGINE_NAME=tavily
WEB_SEARCH_API_KEY=实际的_搜索_API_Key
WEB_SEARCH_URL=https://api.tavily.com
```

不得代替用户写入或修改 `.env`。提醒用户不得保留 `your_openai_api_key_here`、`your_tavily_api_key_here` 等占位值；使用其他受支持的 LLM 或搜索引擎时，按 `.env.example` 中对应示例填写完整配置。

### 3. 检查配置并决定是否执行

运行研究任务前检查以下内容，但不要输出任何配置值：

- `.env` 文件存在。
- `LLM_MODEL_NAME`、`LLM_MODEL_TYPE`、`LLM_BASE_URL`、`LLM_API_KEY`、`WEB_SEARCH_ENGINE_NAME`、`WEB_SEARCH_API_KEY` 和 `WEB_SEARCH_URL` 均已配置。
- 必需参数不为空，且 API Key 不是 `.env.example` 中的示例占位值。

任一检查不通过时，停止当前执行流程并提示用户：

> 检测到 `.env` 文件不存在或存在未配置参数。请先在 openJiuwen-DeepSearch 技能文件夹中自行创建或编辑 `.env`，参考 `.env.example` 完成所有必需配置；配置完成后再重新执行该技能。请勿在对话中发送 API Key。

只有全部检查通过后，才继续执行深度研究命令。

## 执行深度研究

### 命令行执行（推荐）

```bash
uv run "scripts\main.py" --mode query --query "AI手机行业研究报告"
```

### 示例场景

#### 金融分析研报

```bash
uv run "scripts\main.py" --mode query --query "美联储2025年降息对A股科技板块的影响"
```

#### 学术与政策研究

```bash
uv run "scripts\main.py" --mode query --query "中国'新质生产力'政策对制造业中小企业的影响"
```

#### 行业分析

```bash
uv run "scripts\main.py" --mode query --query "2025年新能源汽车行业发展趋势分析"
```

## 停止当前研究任务

停止操作优先于环境和 `.env` 检查。用户要求停止时，由主 Agent 立即执行以下流程：

1. 读取技能文件夹根目录的 `PID.info` 并解析 PID。文件不存在、内容无效或 PID 对应进程已经不存在时，告知用户当前没有正在运行的可识别任务；不得声称本次操作成功终止了任务。
2. 在终止前检查 PID 对应进程的命令行，确认其中包含当前技能文件夹的 `scripts/main.py`。如果 PID 已被其他进程复用或无法确认进程身份，拒绝终止并向用户说明原因，避免误杀无关进程。
3. 终止整个进程树，而不是只终止 `PID.info` 中的根进程：
   - Windows：执行 `taskkill /PID <PID> /T /F`，其中 `/T` 表示同时终止全部子进程。
   - Linux/macOS：后台进程使用独立会话启动，先执行 `kill -TERM -<PID>` 终止整个进程组；等待后仍存活时执行 `kill -KILL -<PID>`。
4. 终止命令返回后等待至少 1 秒，连续检查两次根 PID 是否仍存在；两次检查间隔至少 1 秒。Windows 使用 `Get-Process -Id <PID> -ErrorAction SilentlyContinue`，Linux/macOS 使用 `kill -0 <PID>` 或 `ps -p <PID>`。
5. 只有终止命令成功且连续两次都确认进程不存在时，才能删除 `PID.info`、清理对应定时监测、更新 todo list，并告诉用户“研究任务已停止”。
6. 如果终止命令失败、权限不足、检查时进程仍存在或无法验证结果，必须明确告诉用户停止尚未成功，任务可能仍在运行；不得删除 `PID.info`，不得清理监测任务，也不得回复“已停止”。应继续诊断或在需要更高权限时请求用户授权。

不得把“已发送终止命令”等同于“任务已停止”，也不得在完成退出验证前向用户确认停止成功。

## 可选环境变量

| 变量名                      | 说明            | 默认值        |
| ------------------------ | ------------- | ---------- |
| `MAX_WEB_SEARCH_RESULTS` | 单次搜索最大返回结果数   | `5`        |
| `EXECUTION_METHOD`       | workflow 执行方式 | `parallel` |

### 执行方式

- **parallel**：并行执行（默认，推荐）
- **dependency_driving**：依赖驱动执行

## 输出结果

### 日志输出

- 日志目录：`openJiuwen-DeepSearch技能文件夹根目录/output/logs/`
- 结果目录：`openJiuwen-DeepSearch技能文件夹根目录/output/reports/`

### 报告输出

最终研究报告会以流式的方式输出到到控制台，包含：

- 查询规划结果
- 信息收集过程
- 理解分析内容
- 最终生成的报告

## 错误处理

### 常见错误

1. **缺少必需的环境变量**
   
   ```
   缺少必需的环境变量: LLM_API_KEY, WEB_SEARCH_API_KEY
   ```
   
    **处理方式**：停止执行并提示用户自行编辑 `.env`，完成缺失参数后重新运行；不得代替用户填写，也不得要求用户在对话中发送 API Key

2. **API Key 无效**
   
   ```
   Error: Invalid API key
   ```
   
    **处理方式**：停止执行并提示用户自行检查、更新 `.env` 中的 API Key 后重新运行；不得输出已配置的值

## 注意事项

1. **首次运行必须准备环境和配置**：先创建技能文件夹根目录下的 `.venv` 并安装依赖；`.env` 由用户自行创建和编辑，后续运行复用现有 `.venv` 和 `.env`
2. **查询内容**：查询内容支持空格，无需额外引号
3. **技能移植性**：技能支持任意位置复制，无路径硬编码依赖