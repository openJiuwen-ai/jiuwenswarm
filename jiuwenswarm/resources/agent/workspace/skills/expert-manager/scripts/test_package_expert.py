#!/usr/bin/env python3
"""package_expert 的单元测试。

对标 WorkBuddy test_package_expert：覆盖 output_dir 排除、拒绝自打包、
大小守卫删除不完整 zip 等产品级守卫逻辑。

运行:
    python3 -m unittest test_package_expert
    python3 test_package_expert.py
"""
import tempfile
import unittest
import zipfile
from pathlib import Path

import package_expert as packager


class _ValidResult:
    """mock validate_expert 返回的"校验通过"结果。"""
    is_valid = True

    def summary(self):
        return "valid"


class PackageExpertTest(unittest.TestCase):
    def setUp(self):
        self.original_validate_expert = packager.validate_expert
        self.original_max_package_bytes = packager.MAX_PACKAGE_BYTES
        # 跳过真实校验，聚焦打包守卫逻辑
        packager.validate_expert = lambda _expert_path: _ValidResult()

    def tearDown(self):
        packager.validate_expert = self.original_validate_expert
        packager.MAX_PACKAGE_BYTES = self.original_max_package_bytes

    @staticmethod
    def create_expert_dir(root):
        expert_dir = root / "safe-packager"
        expert_dir.mkdir()
        (expert_dir / "manifest.json").write_text('{"packageType":"agent_template"}', encoding="utf-8")
        (expert_dir / "content.txt").write_text("payload", encoding="utf-8")
        return expert_dir

    @staticmethod
    def zip_names(zip_path):
        with zipfile.ZipFile(zip_path) as zip_file:
            return zip_file.namelist()

    def test_excludes_output_dir_inside_expert_dir(self):
        """output_dir 位于专家目录内时，排除其下的旧产物。"""
        with tempfile.TemporaryDirectory() as tmp:
            expert_dir = self.create_expert_dir(Path(tmp))
            output_dir = expert_dir / "dist"
            output_dir.mkdir()
            (output_dir / "old.zip").write_text("old package", encoding="utf-8")

            zip_path = packager.package_expert(expert_dir, output_dir)

            self.assertIsNotNone(zip_path)
            self.assertTrue(zip_path.exists())
            names = self.zip_names(zip_path)
            self.assertIn("safe-packager/manifest.json", names)
            self.assertIn("safe-packager/content.txt", names)
            self.assertFalse(any(name.startswith("safe-packager/dist/") for name in names))

    def test_excludes_root_dist_when_output_dir_is_external(self):
        """output_dir 在外部时，专家目录内自带的 dist/ 仍被排除。"""
        with tempfile.TemporaryDirectory() as tmp:
            expert_dir = self.create_expert_dir(Path(tmp))
            dist_dir = expert_dir / "dist"
            output_dir = Path(tmp) / "external-output"
            dist_dir.mkdir()
            (dist_dir / "old.zip").write_text("old package", encoding="utf-8")

            zip_path = packager.package_expert(expert_dir, output_dir)

            self.assertIsNotNone(zip_path)
            self.assertTrue(zip_path.exists())
            names = self.zip_names(zip_path)
            self.assertIn("safe-packager/manifest.json", names)
            self.assertFalse(any(name.startswith("safe-packager/dist/") for name in names))

    def test_rejects_output_dir_equal_to_expert_dir(self):
        """output_dir 与专家目录相同 → 拒绝（避免自打包）。"""
        with tempfile.TemporaryDirectory() as tmp:
            expert_dir = self.create_expert_dir(Path(tmp))

            zip_path = packager.package_expert(expert_dir, expert_dir)

            self.assertIsNone(zip_path)
            self.assertFalse((expert_dir / "safe-packager.zip").exists())

    def test_size_guard_removes_incomplete_zip(self):
        """超 MAX_PACKAGE_BYTES → 删除不完整 zip 并返回 None。"""
        with tempfile.TemporaryDirectory() as tmp:
            expert_dir = Path(tmp) / "safe-packager"
            output_dir = Path(tmp) / "dist"
            expert_dir.mkdir()
            output_dir.mkdir()
            (expert_dir / "manifest.json").write_text('{"packageType":"agent_template"}', encoding="utf-8")
            (expert_dir / "big.bin").write_text("too large", encoding="utf-8")
            packager.MAX_PACKAGE_BYTES = 2  # 极小上限强制触发

            zip_path = packager.package_expert(expert_dir, output_dir)

            self.assertIsNone(zip_path)
            self.assertFalse((output_dir / "safe-packager.zip").exists())


if __name__ == "__main__":
    unittest.main()
