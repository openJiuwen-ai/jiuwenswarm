# -*- coding: utf-8 -*-
"""docx-pro 依赖自检：确认 python-docx 可用。"""
import sys


def check():
    print("=== docx-pro 依赖检查 ===")
    try:
        import docx  # noqa: F401
        print("  [OK] python-docx 已安装")
    except ImportError:
        print("  [缺] python-docx 未安装")
        print("  修复: pip install python-docx")
        return False
    try:
        import docx.shared
        print("  [OK] python-docx 组件完整")
    except Exception as e:
        print("  [缺] python-docx 异常: %s" % e)
        return False
    print("全部就绪。")
    return True


if __name__ == "__main__":
    sys.exit(0 if check() else 1)
