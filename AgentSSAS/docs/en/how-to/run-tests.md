# Run Tests

This guide explains how to run AgentSSAS unit tests, integration tests, lint tools, and the Phase 3 end-to-end verification script.

## Prerequisites

Change into the AgentSSAS repository root and install the dependencies:

```bash
cd AgentSecurity/AgentSSAS
uv pip install -e .
```

The tests themselves depend on `pytest` and `pytest-asyncio` (declared in the `test` extra of `pyproject.toml`):

```bash
uv pip install -e ".[test]"
```

HTTP mode tests (`tests/agent_ssas/core/integration/test_e2e_http.py`, `test_http_server.py`, `test_remote_backend.py`, etc.) additionally depend on `fastapi`, `uvicorn`, and `httpx`; install the `http` extra:

```bash
uv pip install -e ".[http]"
```

Both extras can also be installed together:

```bash
uv pip install -e ".[test,http]"
```

To run scripts with uv, specify extras via `--extra`, for example:

```bash
uv run --extra http python examples/http_server_demo/start_server.py
```

## Running pytest Directly

```bash
cd AgentSecurity/AgentSSAS
python -m pytest tests/ --tb=short -q
```

`pyproject.toml` already ships the pytest configuration: `testpaths = ["tests"]`, `asyncio_mode = "auto"` (async tests need no per-test marker), and `addopts = "-v --strict-markers --tb=short"`. Therefore, running `pytest` directly from the repository root also collects all cases under `tests/`.

Run a single file or a single case:

```bash
# A single file
python -m pytest tests/agent_ssas/core/framework/test_config.py -q

# A single case
python -m pytest tests/agent_ssas/core/framework/test_config.py::TestAgentSSASConfig::test_from_dict_none_returns_default -q

# Filter by marker (see the markers section below)
python -m pytest tests/ -m unit -q
python -m pytest tests/ -m "not slow" -q
```

## pytest markers

The following markers are declared in `[tool.pytest.ini_options]` of `pyproject.toml` (used together with `--strict-markers`; undeclared markers raise an error):

| Marker | Description |
|--------|-------------|
| `unit` | Fast, deterministic unit tests |
| `integration` | Integration tests |
| `system` | System tests (require a real environment; skipped in CI) |
| `level0` | Smoke/happy path; must be green as a PR gate |
| `level1` | Feature branches/error paths/edge cases |
| `slow` | Long-running tests |

## Using the Makefile

The `Makefile` at the repository root provides a unified entry point (depends on the ruff, pylint, mypy, and codespell toolchain):

```bash
make install      # Install the lint/test toolchain (ruff, pylint, mypy, codespell)
make test         # Run the tests (equivalent to pytest tests/ --tb=short -q)
make lint         # Lint (ruff + pylint + mypy, fault-tolerant execution)
make format       # Format code (ruff format src/ tests/)
make typecheck    # Type check (mypy src/agent_ssas)
```

There are also two helper targets:

```bash
make lint-fix     # Auto-fix the issues that ruff can fix
make check        # Full check (currently lint)
```

On Windows, if make is not installed, run the corresponding commands from the `Makefile` directly, for example `pytest tests/ --tb=short -q` and `ruff check src/ tests/`.

## Phase 3 Verification Script

`tests/agent_ssas/core/integration/test_verify_phase3.py` provides standalone, executable end-to-end verification commands covering three scenarios: inprocess mode, HTTP service mode, and disabled configuration:

```bash
cd AgentSecurity/AgentSSAS
python -m tests.verify_phase3 all
```

Available subcommands:

| Subcommand | Description |
|------------|-------------|
| `inprocess` | Inprocess mode verification: rail registration + event reporting + log generation |
| `http-server` | Start the HTTP server (runs in the foreground; exit with Ctrl+C) |
| `http-client` | HTTP mode verification: sends events to a running server |
| `disabled` | Disabled configuration verification |
| `all` | Runs inprocess + disabled in sequence (excludes http-server/http-client) |

Notes:

- The script points `SSAS_HOME` to a system temporary directory (`$TEMP/agent_ssas/phase3/<tag>`) and cleans old data before each run, so `~/.jiuwenswarm` is never polluted.
- The `http-server` / `http-client` subcommands require the `http` extra dependencies and must be run in two separate terminals.

## Troubleshooting

- **0 cases collected**: make sure the current directory is `AgentSecurity/AgentSSAS` (the directory containing `pyproject.toml`); pytest collects tests based on its `testpaths` setting.
- **HTTP tests fail with `ModuleNotFoundError: fastapi` (or uvicorn/httpx)**: install the `http` extra; see Prerequisites above.
- **Async tests fail or get skipped**: make sure `pytest-asyncio` is installed; `asyncio_mode = "auto"` is already enabled in the configuration.
- **Cleanup fails on Windows due to file locking**: the verification script has a built-in fallback (renaming the locked directory); if it still fails, close the processes holding the SQLite files and retry.

## Related Documentation

- [HTTP API Reference](../reference/http-api.md)
- [Configuration Reference](../reference/configuration.md)
- The `test` target of the repository root [Makefile](../../../Makefile) and the `[tool.pytest.ini_options]` settings in [pyproject.toml](../../../pyproject.toml)
