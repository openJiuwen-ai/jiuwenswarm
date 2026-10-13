# Repository Guidelines

## 项目结构

JiuwenSwarm 是多智能体协作系统，Python 包发布名为 `workswarm`。

- `jiuwenswarm/agents/`：智能体与 Harness；`server/`、`gateway/`：服务与接入层；`channels/`：各端入口。
- `jiuwenswarm/channels/web/frontend/`：React、TypeScript、Vite 前端；静态资源位于 `src/assets/` 和 `public/`。
- `jiuwenbox/`：沙箱；`sdks/`：Python/TypeScript SDK；`packages/jiuwenswarm-tui/`：TUI 包。
- `tests/`：单元、集成和系统测试；`scripts/`：开发与构建脚本；`docs/`、`deploy/`：文档与部署配置；`jiuwenswarm/resources/`：运行配置模板。

## 安装、运行与构建

使用 Python 3.11–3.13 和 `uv`，从仓库根目录执行：

```bash
uv sync                          # 安装依赖，默认包含测试与开发工具
source .venv/bin/activate         # Windows 使用 .venv\Scripts\activate
export PYTHONPATH=.:$PYTHONPATH    # POSIX 环境设置源码路径
make init                        # 初始化 ~/.jiuwenswarm
make start-debug-rebuild          # 构建前端并启动调试服务
make start-debug                  # 复用已有前端构建
make stop                        # 停止调试服务
make build                       # 构建 wheel/sdist
```

前端独立开发时进入前端目录，执行 `npm ci`、`npm run dev`；`npm run build` 执行 TypeScript 编译与 Vite 构建。

## 代码风格与命名

Python 使用四空格缩进、`snake_case` 函数与模块、`PascalCase` 类；参数声明类型，优先 `X | None` 和 `list[X]`。注释使用英文，docstring 遵循 Google 风格；多行参数保留尾逗号，避免海象运算符与动态扩展 `**kwargs`。

前端遵循 `.prettierrc.cjs`：两空格、单引号、分号、120 列；组件使用 `PascalCase`。仓库配置了 Ruff、Pylint、mypy、ESLint 和 Prettier，但本地协作约束禁止运行格式化与静态检查工具。保持修改处的现有风格。

## 测试要求

使用 pytest、pytest-asyncio、pytest-mock 和 pytest-cov。新增测试采用纯函数，文件命名 `test_*.py` 或 `*_test.py`，函数命名 `test_*`；覆盖正常、失败与回归路径。

- `make test-unit`：运行 `tests/unit_tests/`；`make test-integration`：运行 `tests/integration/`。
- `make test`：运行完整测试；`uv run pytest tests/路径/test_example.py`：定向验证。
- pytest 默认生成终端、`htmlcov/`、`coverage.xml` 报告；`TESTING.md` 总体目标为 80%，当前配置未设置强制阈值。
- 前端使用 Node 测试运行器，按 `package.json` 选择 `npm run test:<功能>`，例如 `npm run test:a4p-settings`。

## 提交与 Pull Request

近期历史以 `feat(scope): ...`、`fix(scope): ...`、`refactor(scope): ...` 为主。新增提交使用简洁英文 Conventional Commits，例如 `fix(gateway): preserve session order`。

PR 面向 `develop`，遵循 `.gitcode/` 模板，说明问题、变更与测试结果，关联 Issue（如 `Closes #123`）；UI 修改附截图或录屏，行为变化同步中英文文档。仅提交本次任务相关文件。

检视人、合并人分派及飞书通知由根目录的 `CODEREVIEWER`、`COMMITTER`、`USER.json` 配置，格式与约束见 `.claude/rules/ownership-files.md`。

## 配置与智能体协作

始终使用中文交流。修改子目录前阅读其中的 `AGENTS.md`；同时遵守 `AGENTS.local.md` 的本地约束。不要提交 API 密钥、真实 `.env`、运行日志或覆盖率产物；配置模板放入 `resources/`。涉及文件与进程操作时兼顾 Windows、macOS 和 Linux。
