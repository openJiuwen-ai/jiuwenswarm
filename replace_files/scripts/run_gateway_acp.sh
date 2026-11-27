#!/bin/bash

# 自动获取项目根目录
ROOT=$(cd "$(dirname "$0")/.." && pwd)

# 设置环境变量
export PYTHONPATH="$ROOT"
export PYTHONIOENCODING=utf-8

# 进入项目目录
cd "$ROOT"

# 启动程序（Linux/Mac 虚拟环境路径不同）
"$ROOT/.venv/bin/python" -m jiuwenclaw.channel.acp_channel "$@"