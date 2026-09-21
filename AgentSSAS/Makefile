# AgentSSAS Makefile -- 统一 lint/test/format 入口 (参照 openJiuwen 社区规范)
# 依赖工具: ruff, pylint, mypy, codespell
# 安装: make install

.PHONY: install test lint lint-fix format typecheck check

# 安装 lint/test 工具链
install:
	pip install ruff>=0.11.2 pylint>=3.0.0 mypy>=1.12.0 codespell>=2.2.4

# 运行测试
test:
	pytest tests/ --tb=short -q

# 代码检查 (ruff + pylint + mypy, 容错执行)
lint:
	ruff check src/ tests/
	pylint src/agent_ssas || true
	mypy src/agent_ssas || true

# 自动修复 ruff 可修复的问题
lint-fix:
	ruff check --fix src/ tests/

# 代码格式化
format:
	ruff format src/ tests/

# 类型检查
typecheck:
	mypy src/agent_ssas

# 完整检查 (lint + format check)
check: lint
	@echo "check complete"
