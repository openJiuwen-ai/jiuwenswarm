"""Regression checks for the frozen lazy-adapter startup gate."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_gateway_adapter_bundle as verifier


def test_verifier_requires_a_frozen_process(monkeypatch):
    monkeypatch.setattr(verifier.sys, "frozen", False, raising=False)
    with pytest.raises(RuntimeError, match="frozen executable"):
        verifier.verify_gateway_adapter_bundle()


@pytest.mark.parametrize("missing", [False, True])
def test_verifier_resolves_lazy_exports_before_backend_import(monkeypatch, missing, tmp_path):
    monkeypatch.setattr(verifier.sys, "frozen", True, raising=False)
    exports = SimpleNamespace(__all__=["Adapter"], Adapter=object())
    if missing:
        del exports.Adapter
    imports = []
    def import_module(name):
        if name == "jiuwenswarm.server.runtime.gateway_adapter":
            return exports
        imports.append(name)
    monkeypatch.setattr(verifier, "import_module", import_module)
    monkeypatch.setattr(verifier, "verify_gateway_extensions", lambda: imports.append("extensions"))
    if missing:
        with pytest.raises(AttributeError):
            verifier.verify_gateway_adapter_bundle()
        assert not imports
    else:
        verifier.verify_gateway_adapter_bundle()
        assert imports[0] == "jiuwenswarm.server.agent_ws_server"
        assert "agent_gateway.channel_manager.im_platforms.welink.welink_runtime" in imports
        assert imports[-1] == "extensions"


def test_spec_collects_lazy_gateway_adapters():
    source = (Path(__file__).resolve().parents[2] / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8")
    compact = "".join(source.split())
    assert 'collect_submodules("jiuwenswarm.server.runtime.gateway_adapter")' in compact
    assert compact.count("gateway_adapter_submodules=") == 1
    assert "+gateway_adapter_submodules" in compact


@pytest.fixture
def gateway_extensions(tmp_path, monkeypatch):
    from agent_gateway.extensions.loader import ExtensionLoader

    for name in ("adx", "agentos", "agent_client"):
        root = tmp_path / "extensions" / name
        root.mkdir(parents=True)
        (root / "extension.yaml").write_text(f"name: {name}\n", encoding="utf-8")
        (root / "provider.py").write_text("def register_extensions(registry): pass\n", encoding="utf-8")
        (root / "extension.py").write_text("from .provider import register_extensions\n", encoding="utf-8")
    modules = {
        "agent_gateway": SimpleNamespace(__file__=str(tmp_path / "__init__.py")),
        "agent_gateway.app_gateway": SimpleNamespace(),
        "agent_gateway.extensions.loader": SimpleNamespace(ExtensionLoader=ExtensionLoader),
    }
    monkeypatch.setattr(verifier, "import_module", modules.__getitem__)
    monkeypatch.setattr(verifier, "version", lambda name: "0.0.1")
    # The real loader creates aliases; keep them local to each test.
    for key in list(verifier.sys.modules):
        if key.startswith("agent_gateway.loaded_extension"):
            monkeypatch.delitem(verifier.sys.modules, key)
    yield tmp_path / "extensions"
    for key in list(verifier.sys.modules):
        if key.startswith("agent_gateway.loaded_extension"):
            del verifier.sys.modules[key]


@pytest.mark.filterwarnings("ignore:__package__ != __spec__.parent:ImportWarning")
def test_verifier_loads_physical_extensions_with_relative_imports(gateway_extensions):
    verifier.verify_gateway_extensions()
    prefix = "agent_gateway.loaded_extension."
    loaded = {
        name.removeprefix(prefix)
        for name in verifier.sys.modules
        if name.startswith(prefix) and "." not in name.removeprefix(prefix)
    }
    assert loaded == {"adx", "agentos", "agent_client"}
    for name in loaded:
        module = verifier.sys.modules[prefix + name]
        assert Path(module.__file__) == gateway_extensions / name / "extension.py"
        assert module.register_extensions is verifier.sys.modules[prefix + name + ".provider"].register_extensions


@pytest.mark.parametrize("filename", ["extension.yaml", "extension.py", "provider.py"])
@pytest.mark.filterwarnings("ignore:__package__ != __spec__.parent:ImportWarning")
def test_verifier_rejects_missing_extension_resources(gateway_extensions, filename):
    (gateway_extensions / "adx" / filename).unlink()
    if filename == "provider.py":
        with pytest.raises(ModuleNotFoundError) as exc:
            verifier.verify_gateway_extensions()
        assert exc.value.name == "agent_gateway.loaded_extension.adx.provider"
    else:
        with pytest.raises(RuntimeError, match="Missing frozen Gateway extension resource:") as exc:
            verifier.verify_gateway_extensions()
        assert str(gateway_extensions / "adx" / filename) in str(exc.value)


def test_verifier_rejects_empty_extension_manifest(gateway_extensions):
    (gateway_extensions / "adx" / "extension.yaml").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Empty frozen Gateway extension manifest"):
        verifier.verify_gateway_extensions()


def test_verifier_rejects_missing_registration_entry(gateway_extensions):
    (gateway_extensions / "adx" / "extension.py").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="registration entry"):
        verifier.verify_gateway_extensions()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_launcher_isolates_before_windows_entry_imports(tmp_path, monkeypatch, caplog, exit_code):
    import sys

    user = tmp_path / "user"
    (user / "config").mkdir(parents=True)
    (user / "config" / "logging.yaml").write_text("invalid: [", encoding="utf-8")
    (user / "config" / ".env").write_text("USER_SETTING=keep\n", encoding="utf-8")
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(user))
    monkeypatch.setenv("JIUWENSWARM_HOME", str(user))
    monkeypatch.setenv("AGENTOS_GATEWAY_LOG_DIR", str(user / "logs"))
    before = {p.relative_to(user): p.read_bytes() for p in user.rglob("*") if p.is_file()}
    original_env = verifier.os.environ.copy()
    original_cwd = Path.cwd()
    report = tmp_path / "isolated-root.txt"
    probe = tmp_path / "entry probe.py"
    entry = Path(__file__).resolve().parents[2] / "scripts" / "jiuwenswarm_exe_entry.py"
    probe.write_text(
        "import os, sys, runpy\nfrom pathlib import Path\n"
        f"sys.path[:] = {sys.path!r}\n"
        # Source Python has no PyInstaller pywin32 runtime hook.
        "if os.name == 'nt':\n    import pywintypes\n"
        "sys.frozen = True\n"
        # Execute the real entrypoint's top-level initialization, including
        # Windows npm setup, without dispatching a service or desktop window.
        f"runpy.run_path({str(entry)!r}, run_name='gateway_entry_probe')\n"
        "from scripts.verify_gateway_adapter_bundle import verify_gateway_extensions\n"
        "try:\n    verify_gateway_extensions()\n"
        "except Exception:\n"
        "    import traceback\n"
        "    Path(os.environ['JIUWENSWARM_DATA_DIR'], 'probe-error.log').write_text(traceback.format_exc(), encoding='utf-8')\n"
        "    raise\n"
        "from jiuwenswarm.common.utils import get_user_workspace_dir\n"
        "from agent_gateway.common.paths import get_config_dir\n"
        "root = Path(os.environ['JIUWENSWARM_DATA_DIR'])\n"
        "assert get_user_workspace_dir() == root\n"
        "assert get_config_dir() == root / 'config'\n"
        "assert Path.cwd() == root\n"
        "assert not (root / 'config/logging.yaml').exists()\n"
        "(root / 'config/.env').write_text('MIGRATED=1')\n"
        "(root / 'logs').mkdir(exist_ok=True)\n"
        "(root / 'logs/probe.log').write_text('isolated failure evidence')\n"
        f"Path({str(report)!r}).write_text(str(root))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verifier, "__file__", str(probe))
    assert verifier.run_isolated(Path(sys.executable)) == exit_code
    if exit_code:
        assert "Frozen Gateway verification failed (exit 7)" in caplog.text
        assert "isolated failure evidence" in caplog.text
    else:
        assert not caplog.records
    assert not Path(report.read_text()).exists()
    assert verifier.os.environ == original_env
    assert Path.cwd() == original_cwd
    assert before == {p.relative_to(user): p.read_bytes() for p in user.rglob("*") if p.is_file()}
