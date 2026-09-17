"""extract_path_candidates：从自由文本提取显式路径候选的回归测试。

背景（20260915170942 端到端复跑）：用户 query 明文携带 pptx 路径，
但框架只透传 query 整段，入口节点未提取导致 glob→ask_user fallback。
"""
from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.runtime.tool_utils import (
    extract_path_candidates,
)

# 端到端复跑的真实 query（全角括号 + 中文路径段）
_REAL_QUERY = (
    "帮我总结一下这个 pptx（D:\\Personal\\0530基线\\20页_case_01\\"
    "解读2026年政府工作报告.pptx）的内容"
)


class TestExtractPathCandidatesWindows:
    def test_real_query_with_fullwidth_parens(self):
        assert extract_path_candidates(_REAL_QUERY, suffixes=(".pptx",)) == [
            "D:\\Personal\\0530基线\\20页_case_01\\解读2026年政府工作报告.pptx"
        ]

    def test_forward_slash_drive(self):
        assert extract_path_candidates("打开 D:/docs/a.pptx 看看") == ["D:/docs/a.pptx"]

    def test_suffix_filter_case_insensitive(self):
        assert extract_path_candidates("文件 D:\\x\\a.PPTX") == ["D:\\x\\a.PPTX"]
        assert extract_path_candidates("文件 D:\\x\\a.PPTX", suffixes=(".pptx",)) == [
            "D:\\x\\a.PPTX"
        ]

    def test_suffix_filter_mismatch_dropped(self):
        assert extract_path_candidates("文件 D:\\x\\a.docx", suffixes=(".pptx",)) == []

    def test_no_suffix_returns_all(self):
        # 目录路径（无扩展名）在无后缀过滤时也应返回
        assert extract_path_candidates("模板包在 D:\\packs\\office 模板") == ["D:\\packs\\office"]

    def test_multiple_paths_dedup_preserving_order(self):
        text = "对比 D:\\a\\1.pptx 和 D:\\b\\2.pptx，以 D:\\a\\1.pptx 为准"
        assert extract_path_candidates(text, suffixes=(".pptx",)) == [
            "D:\\a\\1.pptx",
            "D:\\b\\2.pptx",
        ]

    def test_stop_at_quote_and_punctuation(self):
        text = '路径是"D:\\x y\\a.pptx"，谢谢。'
        # 路径含空格：token 于空格处截断为 D:\x，不满足 .pptx 后缀 → 无候选
        assert extract_path_candidates(text, suffixes=(".pptx",)) == []

    def test_bare_drive_skeleton_skipped(self):
        assert extract_path_candidates("盘符 D:\\ 根目录") == []


class TestExtractPathCandidatesUnix:
    def test_unix_absolute(self):
        assert extract_path_candidates("总结 /home/user/报告.pptx") == ["/home/user/报告.pptx"]

    def test_url_protocol_excluded(self):
        assert extract_path_candidates("见 https://example.com/a.pptx 和 http://b.com/x") == []

    def test_relative_path_excluded(self):
        assert extract_path_candidates("请处理 ./a.pptx 或 ../b.pptx") == []

    def test_query_startswith_slash(self):
        assert extract_path_candidates("/abs/a.pptx 总结一下") == ["/abs/a.pptx"]


class TestExtractPathCandidatesEdge:
    def test_empty_and_non_string(self):
        assert extract_path_candidates("") == []
        assert extract_path_candidates(None) == []
        assert extract_path_candidates(123) == []

    def test_no_paths(self):
        assert extract_path_candidates("帮我总结这个 pptx 的内容，文件在工作区") == []

    def test_directory_trailing_separator_kept(self):
        assert extract_path_candidates("输出到 D:\\out\\ 目录") == ["D:\\out\\"]

    def test_unix_dir_trailing_separator_kept(self):
        assert extract_path_candidates("输出到 /tmp/out/ 目录") == ["/tmp/out/"]

    def test_chinese_full_stop_terminates(self):
        # 句号（。）是路径终止字符，token 在其处截断
        assert extract_path_candidates("用 D:\\a\\b.pptx。谢谢") == ["D:\\a\\b.pptx"]
