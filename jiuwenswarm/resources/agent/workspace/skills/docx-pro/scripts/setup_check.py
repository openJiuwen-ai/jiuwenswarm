# -*- coding: utf-8 -*-
"""docx-pro 依赖自检：确认 python-docx 可用。"""
import logging
import sys

logger = logging.getLogger("docx_pro.setup_check")


def check():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logger.info("=== docx-pro 依赖检查 ===")
    try:
        import docx  # noqa: F401
        logger.info("  [OK] python-docx 已安装")
    except ImportError:
        logger.warning("  [缺] python-docx 未安装")
        logger.info("  修复: pip install python-docx")
        return False
    try:
        import docx.shared
        logger.info("  [OK] python-docx 组件完整")
    except Exception as e:
        logger.warning("  [缺] python-docx 异常: %s", e)
        return False
    logger.info("全部就绪。")
    return True


if __name__ == "__main__":
    sys.exit(0 if check() else 1)
