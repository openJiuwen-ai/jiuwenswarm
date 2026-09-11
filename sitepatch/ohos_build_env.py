"""OhOS: pip PEP517 _in_process 子进程 PATH 极短，site 启动时注入并 patch maturin/setuptools_rust。

路径默认值与 scripts/ohos/ohos-env.sh 对齐，优先读环境变量 / 安装时生成的 toolchain.env：
  OHOS_HNP_ROOT, OHOS_HNP_BIN, OHOS_LLVM_BIN, OHOS_RUST_BIN,
  OHOS_USR_LOCAL, OHOS_STORAGE_ROOT, WHEEL_BUILD_ROOT
"""
from __future__ import annotations

import glob
import importlib.abc
import importlib.util
import os
import shutil
import sys


def _hnp_root() -> str:
    explicit = os.environ.get("OHOS_HNP_ROOT")
    if explicit:
        return explicit.rstrip("/\\")
    hnp_bin = os.environ.get("OHOS_HNP_BIN")
    if hnp_bin:
        return os.path.dirname(hnp_bin.rstrip("/\\"))
    return "/data/service/hnp"


def _default_hnp_bin() -> str:
    return os.environ.get("OHOS_HNP_BIN") or os.path.join(_hnp_root(), "bin")


def _default_usr_local() -> str:
    explicit = os.environ.get("OHOS_USR_LOCAL")
    if explicit:
        return explicit
    storage = os.environ.get("OHOS_STORAGE_ROOT")
    if storage:
        return os.path.join(storage, "usr", "local")
    home = os.environ.get("HOME") or ""
    if home:
        return os.path.join(home, "usr", "local")
    return "/usr/local"


def _default_rust_bin() -> str:
    explicit = os.environ.get("OHOS_RUST_BIN")
    if explicit:
        return explicit.rstrip("/\\")
    home = os.environ.get("HOME") or ""
    return os.path.join(home, "usr", "rust-1.95.0-aarch64-unknown-linux-ohos", "bin")


def _discover_llvm_bin() -> str:
    explicit = os.environ.get("OHOS_LLVM_BIN")
    if explicit and os.path.isdir(explicit):
        return explicit
    pattern = os.path.join(_hnp_root(), "ohos-sdk.org", "ohos-sdk_*", "ohos", "native", "llvm", "bin")
    matches = sorted(glob.glob(pattern))
    for match in matches:
        if os.path.isdir(match):
            return match
    return ""


def _discover_hnp_pkg_dirs() -> list[str]:
    """libxml2/libxslt 等 HNP cmd-pkgs 目录（与 ohos-env.sh ohos_hnp_pkg_glob_roots 对齐）。"""
    custom = os.environ.get("OHOS_HNP_PKG_ROOTS", "").strip()
    if custom:
        return [d for d in custom.split() if d and os.path.isdir(d)]
    out: list[str] = []
    hnp = _hnp_root()
    for pattern in (
        os.path.join(hnp, "libxml2.org", "libxml2_*"),
        os.path.join(hnp, "libxslt.org", "libxslt_*"),
    ):
        for match in sorted(glob.glob(pattern)):
            if os.path.isdir(match):
                out.append(match)
    return out


def _apply_ohos_config_defaults() -> None:
    """未显式设置时补全 OHOS_* 默认值（不覆盖已有 env / toolchain.env）。"""
    defaults = {
        "OHOS_HNP_ROOT": _hnp_root(),
        "OHOS_HNP_BIN": _default_hnp_bin(),
        "OHOS_USR_LOCAL": _default_usr_local(),
        "OHOS_RUST_BIN": _default_rust_bin(),
    }
    llvm = _discover_llvm_bin()
    if llvm:
        defaults["OHOS_LLVM_BIN"] = llvm
    for key, val in defaults.items():
        if val and not os.environ.get(key):
            os.environ[key] = val

    if not os.environ.get("OPENSSL_DIR"):
        usr_local = os.environ.get("OHOS_USR_LOCAL") or _default_usr_local()
        pc = os.path.join(usr_local, "lib", "pkgconfig", "openssl.pc")
        if os.path.isfile(pc):
            os.environ["OPENSSL_DIR"] = usr_local

    # PKG_CONFIG_PATH / LD_LIBRARY_PATH：补 HNP 包与 usr/local（不覆盖已有值）
    pkg_dirs: list[str] = []
    ld_dirs: list[str] = []
    usr_local = os.environ.get("OHOS_USR_LOCAL") or _default_usr_local()
    for base in [usr_local, *_discover_hnp_pkg_dirs()]:
        pc_dir = os.path.join(base, "lib", "pkgconfig")
        lib_dir = os.path.join(base, "lib")
        if os.path.isdir(pc_dir):
            pkg_dirs.append(pc_dir)
        if os.path.isdir(lib_dir):
            ld_dirs.append(lib_dir)
    if pkg_dirs:
        cur = os.environ.get("PKG_CONFIG_PATH", "")
        extra = os.pathsep.join(d for d in pkg_dirs if d not in cur.split(os.pathsep))
        if extra:
            os.environ["PKG_CONFIG_PATH"] = extra + (os.pathsep + cur if cur else "")
    if ld_dirs:
        cur = os.environ.get("LD_LIBRARY_PATH", "")
        extra = os.pathsep.join(d for d in ld_dirs if d not in cur.split(os.pathsep))
        if extra:
            os.environ["LD_LIBRARY_PATH"] = extra + (os.pathsep + cur if cur else "")


def _real_executable(path: str | None) -> str | None:
    if not path:
        return None
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return None
    if os.path.isfile(resolved) and os.access(resolved, os.X_OK):
        return resolved
    return None


def _tool_search_dirs() -> list[str]:
    root = os.environ.get("WHEEL_BUILD_ROOT") or ""
    home = os.environ.get("HOME") or ""
    hnp_bin = os.environ.get("OHOS_HNP_BIN") or _default_hnp_bin()
    llvm_bin = os.environ.get("OHOS_LLVM_BIN") or _discover_llvm_bin()
    rust_bin = os.environ.get("OHOS_RUST_BIN") or _default_rust_bin()
    usr_local_bin = os.path.join(os.environ.get("OHOS_USR_LOCAL") or _default_usr_local(), "bin")
    dirs: list[str] = []
    if root:
        dirs.extend(
            [
                os.path.join(root, "cargo", "bin"),
                os.path.join(root, "bin"),
            ]
        )
    if home:
        dirs.extend(
            [
                rust_bin,
                os.path.join(home, ".cargo", "bin"),
                os.path.join(home, ".local", "bin"),
                usr_local_bin,
            ]
        )
    dirs.append(hnp_bin)
    if llvm_bin:
        dirs.append(llvm_bin)
    for pkg_root in _discover_hnp_pkg_dirs():
        bindir = os.path.join(pkg_root, "bin")
        if os.path.isdir(bindir):
            dirs.append(bindir)
    pybin = os.path.dirname(sys.executable)
    if pybin:
        dirs.append(pybin)
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def _load_env_file(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key and val:
                os.environ[key] = val


def _find_tool(name: str) -> str | None:
    explicit = _real_executable(os.environ.get(name.upper()))
    if explicit:
        return explicit
    found = _real_executable(shutil.which(name))
    if found:
        return found
    for d in _tool_search_dirs():
        p = _real_executable(os.path.join(d, name))
        if p:
            return p
    return None


def _prepend_path(*dirs: str) -> None:
    parts = [d for d in dirs if d and os.path.isdir(d)]
    if not parts:
        return
    cur = os.environ.get("PATH", "")
    prefix = os.pathsep.join(parts)
    if cur:
        if not any(cur.startswith(p + os.pathsep) or cur == p for p in parts):
            os.environ["PATH"] = prefix + os.pathsep + cur
    else:
        os.environ["PATH"] = prefix


def _toolchain_env_candidates() -> list[str]:
    out: list[str] = []
    explicit = os.environ.get("OHOS_TOOLCHAIN_ENV")
    if explicit:
        out.append(explicit)
    root = os.environ.get("WHEEL_BUILD_ROOT")
    if root:
        out.append(os.path.join(root, "sitepatch", "toolchain.env"))
    return out


def _patch_maturin_module(mod) -> None:
    maturin_bin = _real_executable(os.environ.get("MATURIN")) or _find_tool("maturin")
    if not maturin_bin:
        return
    os.environ["MATURIN"] = maturin_bin

    def _get_maturin_executable() -> list[str]:
        return [maturin_bin]

    mod._get_maturin_executable = _get_maturin_executable


def _patch_setuptools_rust_module(mod) -> None:
    rustc_bin = _real_executable(os.environ.get("RUSTC")) or _find_tool("rustc")
    if not rustc_bin:
        return
    os.environ["RUSTC"] = rustc_bin
    cargo_bin = _real_executable(os.environ.get("CARGO")) or _find_tool("cargo")
    if cargo_bin:
        os.environ["CARGO"] = cargo_bin

    orig = mod.find_rust_compiler

    def find_rust_compiler():  # noqa: N802 - match setuptools-rust API
        explicit = _real_executable(os.environ.get("RUSTC"))
        if explicit:
            return explicit
        return orig()

    mod.find_rust_compiler = find_rust_compiler

    # 也立即 patch rustc_info（如果已经加载）
    try:
        from setuptools_rust import rustc_info as _ri
        _patch_setuptools_rust_rustc_info(_ri)
    except ImportError:
        pass


def _patch_setuptools_rust_rustc_info(mod) -> None:
    """Patch setuptools_rust.rustc_info so all rustc subprocess calls
    use RUSTC env var instead of bare 'rustc' (which may not be on PATH)."""
    rustc_bin = _real_executable(os.environ.get("RUSTC")) or _find_tool("rustc")
    if not rustc_bin:
        return

    rustc_dir = os.path.dirname(rustc_bin)

    def _make_rust_subprocess(cmd_args_fn):
        """Return a patched function that calls rustc with correct PATH.
        cmd_args_fn(extra_args) returns the full command list."""
        import subprocess as _sp

        def _patched(*args, **kwargs):
            # All these functions accept (env=...) as kwarg
            env_arg = kwargs.get("env") or (args[0] if args else None)
            out_env = dict(os.environ)
            if env_arg:
                try:
                    out_env.update(env_arg)
                except (TypeError, ValueError):
                    if hasattr(env_arg, "items"):
                        out_env.update(dict(env_arg.items()))
                    elif hasattr(env_arg, "__getitem__"):
                        try:
                            out_env.update(dict(env_arg))
                        except Exception:
                            pass
            out_env["PATH"] = rustc_dir + os.pathsep + out_env.get("PATH", "")
            out_env["RUSTC"] = rustc_bin
            kwargs["env"] = out_env
            # Replace "rustc" with absolute path in cmd
            cmd = cmd_args_fn()
            if cmd and cmd[0] == "rustc":
                cmd = [rustc_bin] + cmd[1:]
            return _sp.check_output(cmd, text=True, **{k: v for k, v in kwargs.items() if k != "env"} if False else None)  # noqa

        return _patched

    # Simpler approach: just replace the functions that call rustc
    import subprocess as _sp

    def _patched_rust_version(env=None):
        out_env = dict(os.environ)
        if env:
            try:
                out_env.update(env)
            except (TypeError, ValueError):
                if hasattr(env, "items"):
                    out_env.update(dict(env.items()))
        out_env["PATH"] = rustc_dir + os.pathsep + out_env.get("PATH", "")
        return _sp.check_output([rustc_bin, "-V"], env=out_env, text=True).strip()

    def _patched_rust_version_verbose(env=None):
        out_env = dict(os.environ)
        if env:
            try:
                out_env.update(env)
            except (TypeError, ValueError):
                if hasattr(env, "items"):
                    out_env.update(dict(env.items()))
        out_env["PATH"] = rustc_dir + os.pathsep + out_env.get("PATH", "")
        return _sp.check_output([rustc_bin, "-Vv"], env=out_env, text=True).strip()

    def _patched_get_rust_target_info(target_triple=None, env=None):
        cmd = [rustc_bin, "--print", "cfg"]
        if target_triple:
            if target_triple.endswith(".json"):
                cmd.extend(["-Z", "unstable-options"])
            cmd.extend(["--target", target_triple.split(".")[0]])
        out_env = dict(os.environ)
        if env:
            try:
                out_env.update(env)
            except (TypeError, ValueError):
                if hasattr(env, "items"):
                    out_env.update(dict(env.items()))
        out_env["PATH"] = rustc_dir + os.pathsep + out_env.get("PATH", "")
        output = _sp.check_output(cmd, env=out_env, text=True)
        return output.splitlines()

    def _patched_get_rust_target_list(env=None):
        out_env = dict(os.environ)
        if env:
            try:
                out_env.update(env)
            except (TypeError, ValueError):
                if hasattr(env, "items"):
                    out_env.update(dict(env.items()))
        out_env["PATH"] = rustc_dir + os.pathsep + out_env.get("PATH", "")
        output = _sp.check_output([rustc_bin, "--print", "target-list"], env=out_env, text=True)
        return output.splitlines()

    # Apply patches
    if hasattr(mod, "_rust_version"):
        mod._rust_version = _patched_rust_version
        _grv = getattr(mod, "get_rust_version", None)
        if _grv and hasattr(_grv, "cache_clear"):
            _grv.cache_clear()

    if hasattr(mod, "_rust_version_verbose"):
        mod._rust_version_verbose = _patched_rust_version_verbose

    if hasattr(mod, "get_rust_target_info"):
        mod.get_rust_target_info = _patched_get_rust_target_info
        # Clear lru_cache if present
        _grti = getattr(mod, "get_rust_target_info", None)
        if _grti and hasattr(_grti, "cache_clear"):
            _grti.cache_clear()
        mod.get_rust_target_info = _patched_get_rust_target_info

    if hasattr(mod, "get_rust_target_list"):
        mod.get_rust_target_list = _patched_get_rust_target_list
        _grtl = getattr(mod, "get_rust_target_list", None)
        if _grtl and hasattr(_grtl, "cache_clear"):
            _grtl.cache_clear()
        mod.get_rust_target_list = _patched_get_rust_target_list


class _LazyPep517Patcher(importlib.abc.MetaPathFinder):
    _TARGETS = {
        "maturin": _patch_maturin_module,
        "setuptools_rust.rust_extension": _patch_setuptools_rust_module,
        "setuptools_rust.rustc_info": _patch_setuptools_rust_rustc_info,
    }

    def find_spec(self, fullname, path, target=None):  # noqa: ARG002
        patcher = self._TARGETS.get(fullname)
        if patcher is None:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            spec = find_spec(fullname, path, target)
            if spec is None or spec.loader is None:
                continue
            loader = spec.loader
            orig_exec = loader.exec_module

            def exec_module(module, _patcher=patcher, _orig_exec=orig_exec):
                _orig_exec(module)
                _patcher(module)

            loader.exec_module = exec_module  # type: ignore[method-assign]
            return spec
        return None


def _install_lazy_pep517_patchers() -> None:
    if any(isinstance(f, _LazyPep517Patcher) for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _LazyPep517Patcher())
    for name, patcher in _LazyPep517Patcher._TARGETS.items():
        if name in sys.modules:
            patcher(sys.modules[name])


def apply() -> None:
    import sys as _sys
    _diag = []
    for path in _toolchain_env_candidates():
        if path and os.path.isfile(path):
            _load_env_file(path)
            _diag.append(f"loaded env: {path}")
            break

    _apply_ohos_config_defaults()
    _diag.append(f"OHOS_HNP_BIN={os.environ.get('OHOS_HNP_BIN', 'unset')}")

    _prepend_path(*_tool_search_dirs())

    rustc = _real_executable(os.environ.get("RUSTC")) or _find_tool("rustc")
    if rustc:
        os.environ["RUSTC"] = rustc
        _diag.append(f"RUSTC={rustc}")
        cargo = _real_executable(os.environ.get("CARGO")) or _find_tool("cargo")
        if not cargo:
            candidate = os.path.join(os.path.dirname(rustc), "cargo")
            cargo = _real_executable(candidate)
        if cargo:
            os.environ["CARGO"] = cargo
            _diag.append(f"CARGO={cargo}")
        else:
            _diag.append("CARGO=NOT_FOUND")
    else:
        _diag.append("RUSTC=NOT_FOUND")
        # 即使 RUSTC 没找到，也尝试找 cargo
        cargo = _real_executable(os.environ.get("CARGO")) or _find_tool("cargo")
        if cargo:
            os.environ["CARGO"] = cargo
            _diag.append(f"CARGO={cargo} (without RUSTC)")

    maturin = _real_executable(os.environ.get("MATURIN")) or _find_tool("maturin")
    if maturin:
        os.environ["MATURIN"] = maturin
        _diag.append(f"MATURIN={maturin}")

    for var, name in (("CC", "clang"), ("CXX", "clang++")):
        if not os.environ.get(var):
            p = _find_tool(name)
            if p:
                os.environ[var] = p

    _install_lazy_pep517_patchers()

    if "maturin" in sys.modules:
        _patch_maturin_module(sys.modules["maturin"])
    try:
        import setuptools_rust.rust_extension as sr_mod

        _patch_setuptools_rust_module(sr_mod)
        _diag.append("setuptools_rust patched")
    except ImportError:
        _diag.append("setuptools_rust NOT importable")

    # 输出诊断到 stderr
    _sys.stderr.write("[ohos_build_env] " + " | ".join(_diag) + "\n")
    _sys.stderr.flush()


apply()
