#!/usr/bin/env python3
"""从 officeClaw 各 pyproject.toml 生成「直接依赖」清单，供鸿蒙逐包安装验证使用。

输出 TSV（stdout）:
  project\tcategory\tpip_spec\timport_module\tnote

每条 pip_spec 会在 sequential 模式中单独 pip install 一次（含传递依赖，无 --no-deps）。

Profile:
  all (默认) — agent-core 全量 optional + jiuwenswarm + relay-claw wheelhouse
  jiuwenswarm-runtime — 仅「跑 jiuwenswarm」闭包：openjiuwen(agent-core) core +
    jiuwenswarm core + 运行时 optional(a2a/desktop/shell-ast/distribute)，
    并前置常见 native 传递依赖行（pydantic_core、rpds-py 等）便于单独记失败。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

# pip 包名 -> import 模块（与 verify-ohos-openjiuwen-deps 对齐）
IMPORT_BY_PACKAGE: dict[str, str] = {
    "beautifulsoup4": "bs4",
    "python-docx": "docx",
    "python-dotenv": "dotenv",
    "pycryptodome": "Crypto",
    "python-dateutil": "dateutil",
    "python-json-logger": "pythonjsonlogger",
    "mermaid-py": "mermaid",
    "json-repair": "json_repair",
    "markdown-it-py": "markdown_it",
    "mathml2omml": "mathml2omml",
    "ruamel.yaml": "ruamel.yaml",
    "google-genai": "google.genai",
    "discord.py": "discord",
    "skillnet-ai": "skillnet",
    "python-telegram-bot": "telegram",
    "python-socks": "python_socks",
    "python-multipart": "multipart",
    "pyyaml": "yaml",
    "PyYAML": "yaml",
    "python-pptx": "pptx",
    "pillow": "PIL",
    "mem0ai": "mem0",
    "agent-sandbox": "agent_sandbox",
    "dingtalk-stream": "dingtalk_stream",
    "wecom-aibot-sdk": "wecom_aibot_sdk",
    "lark-oapi": "lark_oapi",
    "sqlite-vec": "sqlite_vec",
    "tree-sitter-bash": "tree_sitter_bash",
    "tree-sitter": "tree_sitter",
    "async-gaussdb": "async_gaussdb",
    "psycopg2-binary": "psycopg2",
    "elasticsearch": "elasticsearch",
    "openjiuwen": "openjiuwen",
    "jiuwenswarm": "jiuwenswarm",
    "jiuwenswarm-tui": "jiuwenswarm_tui",
    "jiuwenclaw": "jiuwenclaw",
    "markitdown": "markitdown",
    "pulsar-client": "pulsar",
    "sse-starlette": "sse_starlette",
    "opentelemetry-exporter-otlp-proto-grpc": "opentelemetry.exporter.otlp.proto.grpc",
    "opentelemetry-exporter-otlp-proto-http": "opentelemetry.exporter.otlp.proto.http",
    "opentelemetry-sdk": "opentelemetry.sdk",
    "opentelemetry-api": "opentelemetry",
    "opentelemetry-proto": "opentelemetry.proto",
    "uvicorn": "uvicorn",
    "a2a-sdk": "a2a",
    "pydantic_core": "pydantic_core",
    "rpds-py": "rpds",
    "cryptography": "cryptography",
    "grpcio": "grpcio",
    "tokenizers": "tokenizers",
    "safetensors": "safetensors",
    "cffi": "cffi",
    "orjson": "orjson",
    "jiter": "jiter",
    "lxml": "lxml",
    "lxml-html-clean": "lxml_html_clean",
    "lxml_html_clean": "lxml_html_clean",
    "trafilatura": "trafilatura",
    "watchfiles": "watchfiles",
    "httptools": "httptools",
    "onnxruntime": "onnxruntime",
    "protobuf": "google.protobuf",
}

# 鸿蒙上常见 native/编译型传递依赖 — 单独成行，便于 summary 里看到子依赖成败
# (parent 仅作 note，安装时仍会拉完整传递树)
TRANSITIVE_NATIVE_P0: list[tuple[str, str, str]] = [
    ("pydantic_core>=2.0", "pydantic_core", "parent:pydantic,fastapi"),
    ("rpds-py>=0.18", "rpds", "parent:mcp,jsonschema"),
    ("cryptography>=42", "cryptography", "parent:pyjwt,urllib3,…"),
    ("grpcio>=1.60", "grpcio", "parent:opentelemetry,chroma"),
    ("cffi>=1.16", "cffi", "parent:cryptography"),
    ("tokenizers>=0.20", "tokenizers", "parent:transformers"),
    ("safetensors>=0.4", "safetensors", "parent:transformers"),
    ("orjson>=3.9", "orjson", "parent:fastmcp"),
    ("jiter>=0.5", "jiter", "parent:pydantic"),
    ("watchfiles>=0.20", "watchfiles", "parent:uvicorn[standard]"),
    ("httptools>=0.6", "httptools", "parent:uvicorn[standard]"),
    ("onnxruntime>=1.16", "onnxruntime", "parent:chromadb"),
]

TRANSITIVE_NATIVE_AGENTSERVER: list[tuple[str, str, str]] = [
    ("pydantic-core>=2.46.0", "pydantic_core", "preload:ohos-wheel-build/wheels"),
    ("rpds-py>=0.18", "rpds", "parent:jsonschema"),
    ("cryptography>=48.0.0,<49", "cryptography", "preload:ohos wheel;abi3"),
    ("jiter>=0.5", "jiter", "preload:ohos-wheel-build/wheels"),
    ("cffi>=1.16", "cffi", "parent:cryptography"),
    ("lxml>=6.1.0,<6.1.1", "lxml", "parent:trafilatura;native;ohos-wheel"),
]

# Phase 3：fastmcp → fakeredis[lua] → lupa（native，需 ohos wheel）
AGENTCORE_NATIVE_PRELOAD: list[tuple[str, str, str]] = [
    ("lupa>=2.8", "lupa.luajit21", "preload:ohos-wheel-build/wheels;before:fastmcp;parent:fakeredis[lua]"),
]

# 已废弃：agentcore-minimal 改从 agent-core/harmonyos/pyproject.toml 读取（见 collect_agentcore_minimal）
AGENTCORE_MINIMAL_SPECS: list[tuple[str, str, str]] = []

OPENJIUWEN_EXTRA_SPECS: dict[str, list[str]] = {
    "postgres": ["asyncpg>=0.30.0"],
    "zmq": ["pyzmq>=26.0.0", "tornado>=6.1"],
}

JIUWENSWARM_RUNTIME_EXTRAS = frozenset({"a2a", "desktop", "shell-ast", "distribute"})
JIUWENSWARM_SKIP_EXTRAS = frozenset({"test", "dev", "tui", "all"})

# 仅引用其它 extra 的 meta 名，跳过
SKIP_OPTIONAL_GROUPS = frozenset(
    {
        "default",
        "all",
        "all-mq",
        "all-storage",
        "all-vector",
    }
)

RELAY_WHEELHOUSE = [
    "setuptools",
    "wheel",
    "httpx>=0.27.0",
    "python-pptx",
    "openpyxl",
    "python-docx",
    "requests",
    "pillow",
    "PyYAML",
    "xlsxwriter",
    "pypdf",
    "pdfplumber",
    "pandas",
    "reportlab",
    "markitdown",
]


def load_pyproject(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def package_name_from_spec(spec: str) -> str:
    spec = spec.strip()
    if spec.startswith("-e "):
        return Path(spec.split(maxsplit=1)[1]).name
    # name @ url
    if " @ " in spec:
        return spec.split(" @ ", 1)[0].strip()
    # name[extra]>=ver
    m = re.match(r"^([A-Za-z0-9_.-]+)", spec)
    return m.group(1) if m else spec


def is_leaf_spec(spec: str) -> bool:
    s = spec.strip()
    if not s:
        return False
    if s.startswith("openjiuwen["):
        return False
    if re.fullmatch(r"openjiuwen\[[^\]]+\]", s):
        return False
    return True


def import_module_for(spec: str) -> str:
    pkg = package_name_from_spec(spec)
    if pkg in IMPORT_BY_PACKAGE:
        return IMPORT_BY_PACKAGE[pkg]
    return pkg.replace("-", "_")


def add_row(
    rows: list[tuple[str, str, str, str, str]],
    seen: set[str],
    project: str,
    category: str,
    spec: str,
    note: str = "",
    *,
    dedupe_by_spec: bool = False,
) -> None:
    spec = spec.strip()
    if not spec or not is_leaf_spec(spec):
        return
    key = spec if dedupe_by_spec else f"{project}\0{spec}"
    if key in seen:
        return
    seen.add(key)
    rows.append((project, category, spec, import_module_for(spec), note))


def expand_openjiuwen_extra(spec: str) -> list[str]:
    """openjiuwen[postgres,zmq] -> 具体 PyPI spec 列表。"""
    m = re.fullmatch(r"openjiuwen\[([^\]]+)\]", spec.strip())
    if not m:
        return []
    out: list[str] = []
    for part in m.group(1).split(","):
        name = part.strip()
        out.extend(OPENJIUWEN_EXTRA_SPECS.get(name, []))
    return out


def add_transitive_native_rows(
    rows: list[tuple[str, str, str, str, str]],
    seen: set[str],
) -> None:
    for spec, _mod, note in TRANSITIVE_NATIVE_P0:
        add_row(
            rows,
            seen,
            "transitive-native",
            "p0",
            spec,
            note,
            dedupe_by_spec=True,
        )


def collect_project_deps(
    rows: list[tuple[str, str, str, str, str]],
    seen: set[str],
    project: str,
    pyproject_path: Path,
    *,
    include_optional: bool = True,
    optional_groups: frozenset[str] | None = None,
    skip_optional_groups: frozenset[str] | None = None,
    dedupe_by_spec: bool = False,
) -> None:
    if not pyproject_path.is_file():
        return
    data = load_pyproject(pyproject_path)
    proj = data.get("project", {})
    for spec in proj.get("dependencies") or []:
        s = str(spec)
        if project == "jiuwenswarm" and "openjiuwen" in s and ("git+" in s or "git://" in s):
            continue
        add_row(rows, seen, project, "core", s, dedupe_by_spec=dedupe_by_spec)
    if include_optional:
        opt = proj.get("optional-dependencies") or {}
        for group, specs in opt.items():
            if group in SKIP_OPTIONAL_GROUPS:
                continue
            if skip_optional_groups and group in skip_optional_groups:
                continue
            if optional_groups is not None and group not in optional_groups:
                continue
            if group == "all" and isinstance(specs, list) and any(
                str(s).startswith("openjiuwen[") for s in specs
            ):
                continue
            for spec in specs:
                s = str(spec)
                if s.startswith("openjiuwen["):
                    for leaf in expand_openjiuwen_extra(s):
                        add_row(
                            rows,
                            seen,
                            project,
                            f"extra:{group}",
                            leaf,
                            group,
                            dedupe_by_spec=dedupe_by_spec,
                        )
                    continue
                add_row(
                    rows,
                    seen,
                    project,
                    f"extra:{group}",
                    s,
                    group,
                    dedupe_by_spec=dedupe_by_spec,
                )


def parse_requirements_file(path: Path) -> list[str]:
    specs: list[str] = []
    if not path.is_file():
        return specs
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " #" in line:
            line = line.split(" #", 1)[0].strip()
        if line.startswith("openjiuwen") or line.startswith("pip install"):
            continue
        specs.append(line)
    return specs


def _spec_package_key(spec: str) -> str:
    return package_name_from_spec(spec).lower().replace("_", "-")


def collect_agentserver_minimal(
    rows: list[tuple[str, str, str, str, str]],
    seen: set[str],
    requirements_path: Path,
) -> None:
    """requirements-minimal.txt + AgentServer 所需 native 传递依赖。"""
    req_specs = parse_requirements_file(requirements_path)
    req_keys = {_spec_package_key(s) for s in req_specs}

    for spec, _mod, note in TRANSITIVE_NATIVE_AGENTSERVER:
        if _spec_package_key(spec) in req_keys:
            continue
        add_row(
            rows,
            seen,
            "transitive-native",
            "p0",
            spec,
            note,
            dedupe_by_spec=True,
        )

    for spec in req_specs:
        add_row(
            rows,
            seen,
            "agentserver-minimal",
            "requirements-minimal",
            spec,
            "requirements-minimal.txt",
            dedupe_by_spec=True,
        )


def resolve_harmonyos_pyproject(agent_core: Path, office_claw: Path) -> Path | None:
    openjiuwen_src = os.environ.get("OPENJIUWEN_SRC_DIR", "").strip()
    for candidate in (
        Path(os.environ["HARMONYOS_PYPROJECT"])
        if os.environ.get("HARMONYOS_PYPROJECT")
        else None,
        Path(openjiuwen_src) / "harmonyos" / "pyproject.toml" if openjiuwen_src else None,
        agent_core / "harmonyos" / "pyproject.toml",
        office_claw / "agent-core" / "harmonyos" / "pyproject.toml",
        office_claw / "agent-core_5969" / "harmonyos" / "pyproject.toml",
    ):
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def collect_agentcore_minimal(
    rows: list[tuple[str, str, str, str, str]],
    seen: set[str],
    *,
    harmonyos_path: Path | None,
    requirements_path: Path,
) -> None:
    """openjiuwen --no-deps 后补装 agent-core/harmonyos/pyproject.toml 中 Phase 1 未覆盖的依赖。"""
    for spec, _mod, note in TRANSITIVE_NATIVE_AGENTSERVER:
        if _spec_package_key(spec) not in {"cryptography", "cffi", "rpds-py"}:
            continue
        add_row(
            rows,
            seen,
            "transitive-native",
            "p0",
            spec,
            note,
            dedupe_by_spec=True,
        )

    for spec, _mod, note in AGENTCORE_NATIVE_PRELOAD:
        add_row(
            rows,
            seen,
            "transitive-native",
            "p0",
            spec,
            note,
            dedupe_by_spec=True,
        )

    if harmonyos_path is None or not harmonyos_path.is_file():
        print(
            f"WARN: harmonyos pyproject not found (set AGENT_CORE_PATH); "
            f"fallback AGENTCORE_MINIMAL_SPECS ({len(AGENTCORE_MINIMAL_SPECS)} pkgs)",
            file=sys.stderr,
        )
        for spec, _mod, note in AGENTCORE_MINIMAL_SPECS:
            add_row(
                rows,
                seen,
                "agentcore-minimal",
                "openjiuwen-runtime",
                spec,
                note,
                dedupe_by_spec=True,
            )
        return

    req_keys = {_spec_package_key(s) for s in parse_requirements_file(requirements_path)}
    harmony_deps = load_pyproject(harmonyos_path).get("project", {}).get("dependencies") or []
    rel_note = f"harmonyos/pyproject.toml (minus requirements-minimal)"

    for spec in harmony_deps:
        s = str(spec).strip()
        if not s:
            continue
        if _spec_package_key(s) in req_keys:
            continue
        note = rel_note
        if _spec_package_key(s) == "dashscope":
            note += ";needs:cryptography+openssl"
        if _spec_package_key(s) in {"fastmcp", "mcp"}:
            note += ";tool-protocol"
        add_row(
            rows,
            seen,
            "agentcore-minimal",
            "openjiuwen-harmonyos",
            s,
            note,
            dedupe_by_spec=True,
        )


def collect_jiuwenswarm_runtime(
    rows: list[tuple[str, str, str, str, str]],
    seen: set[str],
    agent_core: Path,
    jiuwen_root: Path,
) -> None:
    """jiuwenswarm 跑起来所需 PyPI 闭包（不含本体 -e / git openjiuwen）。"""
    add_transitive_native_rows(rows, seen)
    collect_project_deps(
        rows,
        seen,
        "agent-core",
        agent_core / "pyproject.toml",
        include_optional=False,
        dedupe_by_spec=True,
    )
    collect_project_deps(
        rows,
        seen,
        "jiuwenswarm",
        jiuwen_root / "pyproject.toml",
        include_optional=True,
        optional_groups=JIUWENSWARM_RUNTIME_EXTRAS,
        skip_optional_groups=JIUWENSWARM_SKIP_EXTRAS,
        dedupe_by_spec=True,
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pypi-only",
        action="store_true",
        help="omit -e editable, git, and path deps (for Harmony device without source tree)",
    )
    parser.add_argument(
        "--profile",
        choices=("all", "jiuwenswarm-runtime", "agentserver-minimal", "agentcore-minimal"),
        default=os.environ.get("MANIFEST_PROFILE", "all"),
        help="all=全 officeClaw; jiuwenswarm-runtime; agentserver-minimal; agentcore-minimal",
    )
    parser.add_argument(
        "--requirements",
        default=os.environ.get("REQUIREMENTS_MINIMAL", ""),
        help="requirements-minimal.txt path (agentserver-minimal / agentcore-minimal diff)",
    )
    parser.add_argument(
        "--harmonyos-pyproject",
        default=os.environ.get("HARMONYOS_PYPROJECT", ""),
        help="agent-core/harmonyos/pyproject.toml (profile agentcore-minimal)",
    )
    args = parser.parse_args()
    pypi_only = args.pypi_only or os.environ.get("PYPI_ONLY") == "1"
    profile = args.profile

    office_claw = Path(os.environ.get("OFFICE_CLAW", Path(__file__).resolve().parents[2].parent))
    repo_root = Path(
        os.environ.get("OHOS_REPO_ROOT")
        or os.environ.get("JIUWENCLAW_PATH")
        or Path(__file__).resolve().parents[2]
    )
    agent_core = Path(os.environ.get("AGENT_CORE_PATH", office_claw / "agent-core"))
    jiuwen_root = Path(os.environ.get("JIUWEN_ROOT", repo_root))
    relay_claw = Path(os.environ.get("RELAY_CLAW_PATH", office_claw / "relay-claw"))
    jiuwenclaw = Path(os.environ.get("JIUWENCLAW_VENDOR_PATH", relay_claw / "vendor" / "jiuwenclaw"))
    tui_path = jiuwen_root / "packages" / "jiuwenswarm-tui"

    rows: list[tuple[str, str, str, str, str]] = []
    seen: set[str] = set()

    if profile == "jiuwenswarm-runtime":
        collect_jiuwenswarm_runtime(rows, seen, agent_core, jiuwen_root)
    elif profile == "agentserver-minimal":
        req_path = Path(args.requirements) if args.requirements else (repo_root / "requirements-minimal.txt")
        collect_agentserver_minimal(rows, seen, req_path)
    elif profile == "agentcore-minimal":
        req_path = Path(args.requirements) if args.requirements else (repo_root / "requirements-minimal.txt")
        harmony_path = (
            Path(args.harmonyos_pyproject)
            if args.harmonyos_pyproject
            else resolve_harmonyos_pyproject(agent_core, office_claw)
        )
        collect_agentcore_minimal(
            rows,
            seen,
            harmonyos_path=harmony_path,
            requirements_path=req_path,
        )
    else:
        collect_project_deps(rows, seen, "agent-core", agent_core / "pyproject.toml")

        if not pypi_only:
            add_row(
                rows,
                seen,
                "agent-core",
                "extra:intelli-router",
                "intelli-router @ git+https://gitcode.com/openJiuwen/agent-protocol.git@feature/intelliRouter#subdirectory=intelli_router",
                "git optional",
            )

        collect_project_deps(rows, seen, "jiuwenswarm", jiuwen_root / "pyproject.toml")
        if not pypi_only and (agent_core / "pyproject.toml").is_file():
            add_row(
                rows,
                seen,
                "jiuwenswarm",
                "core",
                f"-e {agent_core.resolve()}",
                "local openjiuwen replaces git",
            )

        collect_project_deps(rows, seen, "jiuwenswarm-tui", tui_path / "pyproject.toml")

        if not pypi_only and (jiuwenclaw / "pyproject.toml").is_file():
            add_row(
                rows,
                seen,
                "jiuwenclaw",
                "editable",
                f"-e {jiuwenclaw.resolve()}",
                "relay-claw vendor",
            )
            collect_project_deps(rows, seen, "jiuwenclaw", jiuwenclaw / "pyproject.toml")

        for spec in RELAY_WHEELHOUSE:
            add_row(rows, seen, "relay-claw", "wheelhouse", spec, "shared-runtime")

        if not pypi_only:
            if (jiuwen_root / "pyproject.toml").is_file():
                add_row(
                    rows,
                    seen,
                    "jiuwenswarm",
                    "editable",
                    f"-e {jiuwen_root.resolve()}",
                    "project itself",
                )
            if (agent_core / "pyproject.toml").is_file():
                add_row(
                    rows,
                    seen,
                    "agent-core",
                    "editable",
                    f"-e {agent_core.resolve()}",
                    "project itself",
                )

    if pypi_only:
        rows = [
            r
            for r in rows
            if not r[2].strip().startswith("-e ")
            and " @ git+" not in r[2]
            and "git+https" not in r[2]
        ]

    out_path = os.environ.get("MANIFEST_OUT")
    lines = ["project\tcategory\tpip_spec\timport_module\tnote"]
    lines.extend("\t".join(r) for r in rows)
    text = "\n".join(lines) + "\n"
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print(f"# manifest profile={profile}: {len(rows)} rows", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
