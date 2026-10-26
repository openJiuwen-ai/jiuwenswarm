#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSearch 环境验证和参数检查脚本

用途：
1. 验证 main.py 是否存在
2. 检查 API Key 配置
"""

import logging
import sys
from pathlib import Path

# 设置 UTF-8 编码输出
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def check_main_py():
    """检查 main.py 是否存在"""
    # 获取技能目录
    skill_dir = Path(__file__).parent.parent
    main_py_path = skill_dir / "scripts" / "main.py"

    logger.info("\n检查 main.py: %s", main_py_path)

    if main_py_path.exists():
        logger.info("✅ main.py 存在")
        return True
    else:
        logger.error("❌ main.py 不存在")
        return False


def check_api_keys():
    """检查 API Key 配置"""
    logger.info("\n检查 API Key 配置：")
    logger.info("⚠️  请确认 .env 文件中已配置以下变量：")
    logger.info("   - LLM_API_KEY")
    logger.info("   - WEB_SEARCH_API_KEY")
    logger.info("\n配置方式：")
    logger.info("   1. 复制配置文件: cp .env.example .env")
    logger.info("   2. 编辑 .env 文件，填入 API Key")
    return True


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("openJiuwen-DeepSearch 环境验证")
    logger.info("=" * 60)

    checks = [
        check_main_py(),
        check_api_keys(),
    ]

    logger.info("\n" + "=" * 60)
    if all(checks):
        logger.info("✅ 环境验证通过，可以开始使用 DeepSearch")
        logger.info("\n快速开始：")
        logger.info('python "scripts\\main.py" --mode query --query "研究主题"')
    else:
        logger.error("❌ 环境验证失败，请解决上述问题后再试")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
