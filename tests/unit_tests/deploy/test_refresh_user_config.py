# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""refresh_user_config.py 的单元测试。

不依赖 root 或网络：外部 I/O 通过注入的 Runtime 替换，文件操作使用临时目录。
可用标准库运行：

    python -m unittest discover -s tests/unit_tests/deploy -v
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from ruamel.yaml import YAML

MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "deploy" / "yuanrong" / "refresh_user_config.py"
)
_SPEC = importlib.util.spec_from_file_location("refresh_user_config", MODULE_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - 防御
    raise RuntimeError(f"无法加载 {MODULE_PATH}")
mod = importlib.util.module_from_spec(_SPEC)
sys.modules["refresh_user_config"] = mod
_SPEC.loader.exec_module(mod)


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------


def _round_load(path):
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.load(handle)


def _round_dump(path, data):
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    with open(path, "w", encoding="utf-8") as handle:
        yaml.dump(data, handle)


TEMPLATE_TEXT = """\
models:
  defaults:
    - model_id: mdl_default
      model_client_config:
        model_name: default-model
        api_base: https://default.example/v1
sandbox:
  image: swr.example/tool-sandbox:latest
gateway:
  agent_client:
    frontend_endpoint: ""
"""


def _agentos_block(names=("bk-a", "bk-b")):
    lines = ["  agentos:"]
    for name in names:
        lines.extend(
            [
                f"    - model_id: mdl_{name}",
                "      model_client_config:",
                f"        model_name: {name}",
            ]
        )
    return "\n".join(lines) + "\n"


def _user_config_text(agentos_block=None):
    if not agentos_block:
        return TEMPLATE_TEXT
    return TEMPLATE_TEXT.replace("models:\n", "models:\n" + agentos_block, 1)


def make_runtime(**overrides):
    base = {
        "chown": lambda path, uid, gid: None,
        "get_user_ids": lambda name: (1000, 1000),
        "geteuid": lambda: 0,
        "is_tty": lambda: False,
        "load_round": _round_load,
        "dump_round": _round_dump,
    }
    base.update(overrides)
    return replace(mod.Runtime(), **base)


class Workspace:
    """在临时目录里构造一个已初始化的用户。"""

    def __init__(self, root: Path, user_config_text: str = TEMPLATE_TEXT):
        self.root = root
        self.home_base = root / "home"
        self.user_cfg_dir = self.home_base / "testu02" / ".jiuwenswarm" / "config"
        self.user_cfg_dir.mkdir(parents=True)
        self.template = root / "template.yaml"
        self.template.write_text(TEMPLATE_TEXT, encoding="utf-8")
        self.user_cfg = self.user_cfg_dir / "config.yaml"
        self.user_cfg.write_text(user_config_text, encoding="utf-8")

    def options(self, **overrides):
        values = {
            "username": "testu02",
            "home_base": self.home_base,
            "template": self.template,
            "yes": True,
        }
        values.update(overrides)
        return mod.Options(**values)


def run_captured(options, runtime):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mod.run(options, runtime)
    return rc, out.getvalue(), err.getvalue()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


class PureFunctionTests(unittest.TestCase):
    def test_validate_username(self):
        self.assertTrue(mod.validate_username("user_68"))
        self.assertTrue(mod.validate_username("user1"))
        self.assertFalse(mod.validate_username("User"))
        self.assertFalse(mod.validate_username("ab"))
        self.assertFalse(mod.validate_username("../etc"))

    def test_backup_candidate_collision(self):
        cfg = Path("/home/agentos/users/u/.jiuwenswarm/config/config.yaml")
        existing = {Path(str(cfg) + ".bak.20261023-120000")}
        candidate = mod.backup_candidate(
            cfg, "20261023-120000", lambda path: path in existing
        )
        self.assertEqual(str(candidate), str(cfg) + ".bak.20261023-120000-1")

    def test_extract_agentos_states(self):
        self.assertEqual(mod.extract_agentos({}), (mod.ABSENT, None))
        self.assertEqual(mod.extract_agentos({"models": None}), (mod.ABSENT, None))
        self.assertEqual(mod.extract_agentos({"models": {"agentos": None}}), (mod.ABSENT, None))
        self.assertEqual(mod.extract_agentos({"models": {"agentos": []}}), (mod.EMPTY, []))
        entries = [{"model_id": "m1"}]
        self.assertEqual(mod.extract_agentos({"models": {"agentos": entries}}), (mod.MERGED, entries))

    def test_extract_agentos_rejects_invalid(self):
        with self.assertRaises(mod.FatalError):
            mod.extract_agentos({"models": {"agentos": "oops"}})
        with self.assertRaises(mod.FatalError):
            mod.extract_agentos({"models": {"agentos": ["oops"]}})
        with self.assertRaises(mod.FatalError):
            mod.extract_agentos({"models": []})

    def test_normalize_agentos(self):
        entries = [{"model_id": "m1", "is_default": True}]
        self.assertIsNone(mod.normalize_agentos(entries))
        self.assertFalse(entries[0]["is_default"])

    def test_apply_agentos_creates_models(self):
        data = mod.apply_agentos({}, [{"model_id": "m"}])
        self.assertEqual(data["models"]["agentos"], [{"model_id": "m"}])

        target = {"models": {"defaults": []}}
        mod.apply_agentos(target, [{"model_id": "m"}])
        self.assertEqual(target["models"]["defaults"], [])
        self.assertEqual(target["models"]["agentos"], [{"model_id": "m"}])

        with self.assertRaises(mod.FatalError):
            mod.apply_agentos({"models": []}, [])


# ---------------------------------------------------------------------------
# 步骤编排（真实临时文件 + 假 I/O）
# ---------------------------------------------------------------------------


class FlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_dry_run_does_not_write(self):
        ws = Workspace(self.root, _user_config_text(_agentos_block()))
        before = sorted(path.name for path in ws.user_cfg_dir.iterdir())

        rc, out, _ = run_captured(ws.options(dry_run=True), make_runtime())

        self.assertEqual(rc, 0)
        self.assertEqual(sorted(path.name for path in ws.user_cfg_dir.iterdir()), before)
        self.assertIn("将恢复 models.agentos（2 条）", out)

    def test_dry_run_reports_empty_agentos(self):
        ws = Workspace(self.root, _user_config_text("  agentos: []\n"))
        before = sorted(path.name for path in ws.user_cfg_dir.iterdir())

        rc, out, _ = run_captured(ws.options(dry_run=True), make_runtime())

        self.assertEqual(rc, 0)
        self.assertIn("为空列表", out)
        self.assertNotIn("无 models.agentos", out)
        self.assertEqual(sorted(path.name for path in ws.user_cfg_dir.iterdir()), before)

    def test_merge_and_install(self):
        ws = Workspace(self.root, _user_config_text(_agentos_block()))
        chowns = []
        runtime = make_runtime(chown=lambda path, uid, gid: chowns.append((str(path), uid, gid)))

        rc, out, _ = run_captured(ws.options(), runtime)

        self.assertEqual(rc, 0)
        merged = _round_load(ws.user_cfg)
        names = [
            entry["model_client_config"]["model_name"]
            for entry in merged["models"]["agentos"]
        ]
        self.assertEqual(names, ["bk-a", "bk-b"])
        self.assertEqual(
            merged["models"]["defaults"][0]["model_client_config"]["model_name"],
            "default-model",
        )
        self.assertEqual(len(list(ws.user_cfg_dir.glob("config.yaml.bak.*"))), 1)
        self.assertTrue(any(str(ws.user_cfg) in item[0] for item in chowns))
        self.assertIn("配置已更新", out)

    def test_invalid_agentos_keeps_target(self):
        ws = Workspace(self.root, _user_config_text("  agentos: oops\n"))
        before = _sha(ws.user_cfg)

        with self.assertRaises(mod.FatalError):
            run_captured(ws.options(), make_runtime())

        self.assertEqual(_sha(ws.user_cfg), before)
        self.assertEqual(list(ws.user_cfg_dir.glob("config.yaml.new.*")), [])

    def _add_user(self, username, agentos_block=None):
        cfg_dir = self.root / "home" / username / ".jiuwenswarm" / "config"
        cfg_dir.mkdir(parents=True)
        text = _user_config_text(agentos_block) if agentos_block else TEMPLATE_TEXT
        cfg = cfg_dir / "config.yaml"
        cfg.write_text(text, encoding="utf-8")
        return cfg

    def test_all_updates_every_user(self):
        ws = Workspace(self.root, _user_config_text(_agentos_block()))
        other = self._add_user("testu03", _agentos_block(("bk-c",)))

        rc, out, _ = run_captured(
            ws.options(all_users=True, username=""), make_runtime()
        )

        self.assertEqual(rc, 0)
        self.assertIn("成功: 2 / 2", out)
        self.assertEqual(
            [
                entry["model_client_config"]["model_name"]
                for entry in _round_load(ws.user_cfg)["models"]["agentos"]
            ],
            ["bk-a", "bk-b"],
        )
        self.assertEqual(
            [
                entry["model_client_config"]["model_name"]
                for entry in _round_load(other)["models"]["agentos"]
            ],
            ["bk-c"],
        )

    def test_all_skips_failed_user(self):
        ws = Workspace(self.root)
        self._add_user("testu04", "  agentos: oops\n")

        rc, out, err = run_captured(
            ws.options(all_users=True, username=""), make_runtime()
        )

        self.assertEqual(rc, 1)
        self.assertIn("成功: 1 / 2", out)
        self.assertIn("testu04", err)

    def test_all_dry_run_does_not_write(self):
        ws = Workspace(self.root)
        other = self._add_user("testu03")
        before = {ws.user_cfg: ws.user_cfg.read_bytes(), other: other.read_bytes()}

        rc, out, _ = run_captured(ws.options(all_users=True, username="", dry_run=True), make_runtime())

        self.assertEqual(rc, 0)
        self.assertIn("将刷新 2 个用户", out)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_all_without_users_returns_0(self):
        ws = Workspace(self.root)
        empty_base = self.root / "empty"
        empty_base.mkdir()

        rc, out, _ = run_captured(
            ws.options(all_users=True, username="", home_base=empty_base),
            make_runtime(),
        )

        self.assertEqual(rc, 0)
        self.assertIn("没有已初始化的用户", out)

    def test_all_survives_corrupt_yaml(self):
        ws = Workspace(self.root)
        bad_dir = self.root / "home" / "testu05" / ".jiuwenswarm" / "config"
        bad_dir.mkdir(parents=True)
        (bad_dir / "config.yaml").write_text("models: [\n", encoding="utf-8")

        rc, out, err = run_captured(
            ws.options(all_users=True, username="", yes=True),
            make_runtime(),
        )

        self.assertEqual(rc, 1)
        self.assertIn("成功: 1 / 2", out)
        self.assertIn("testu05", err)

    def test_all_survives_unexpected_exception(self):
        ws = Workspace(self.root, _user_config_text(_agentos_block()))
        self._add_user("testu03", _agentos_block(("bk-c",)))

        def boom(path, data):
            raise ValueError("boom")

        rc, out, err = run_captured(
            ws.options(all_users=True, username="", yes=True),
            make_runtime(dump_round=boom),
        )

        self.assertEqual(rc, 1)
        self.assertIn("成功: 0 / 2", out)
        self.assertIn("ValueError: boom", err)

    def test_main_all_with_username_errors(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mod.main(["--all", "testu02", "-y"], runtime=make_runtime())
        self.assertEqual(rc, 1)
        self.assertIn("--all 不接受用户名参数", err.getvalue())

    def test_main_returns_1_for_invalid_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mod.main(["BAD"], runtime=make_runtime())
        self.assertEqual(rc, 1)
        self.assertIn("用户名格式无效", err.getvalue())

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mod.main([], runtime=make_runtime())
        self.assertEqual(rc, 1)
        self.assertIn("未指定用户", err.getvalue())


if __name__ == "__main__":
    unittest.main()
