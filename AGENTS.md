# Repository Guidelines

## Project Structure & Module Organization

JiuwenSwarm is a Python 3.11+ package rooted at `jiuwenswarm/`. Core areas include `agents/`, `cli/`, `gateway/`, `server/`, `channels/`, `symphony/`, and shared helpers in `common/`. `jiuwenbox/` is a packaged companion module with its own `src/` and `tests/`. Web frontend code lives under `jiuwenswarm/channels/web/frontend/`; TUI code lives under `jiuwenswarm/channels/tui/frontend/` and `packages/jiuwenswarm-tui/`. Tests are in `tests/`, with unit, integration, system, symphony, and UI E2E subtrees. Documentation and images are in `docs/` and `docs/assets/`; deployment examples are in `deploy/`.

## Build, Test, and Development Commands

- `pip install -e ".[test]"`: install the Python package with test dependencies.
- `pytest`: run the configured Python test suite with coverage reports for `jiuwenswarm`.
- `pytest tests/unit -m unit`: run a focused subset.
- `python -m build`: build Python distribution artifacts when the `build` package is installed.
- `cd jiuwenswarm && npm run dev`: start the web frontend through Vite.
- `cd jiuwenswarm && npm run build`: generate frontend assets for packaging.
- `cd jiuwenswarm/channels/web/frontend && npm run lint`: lint the React/TypeScript frontend.
- `cd jiuwenswarm/channels/tui/frontend && npm run check`: typecheck, lint, and format-check the TUI frontend.

## Coding Style & Naming Conventions

Python code should follow PEP 8: 4-space indentation, `snake_case` functions/modules, and `PascalCase` classes. Keep imports rooted in `jiuwenswarm` or `jiuwenbox` rather than relying on path side effects. Frontend code uses TypeScript, React components in `PascalCase`, hooks/utilities in `camelCase`, and colocated `.css` files where existing components do so. Use Ruff, pylint, mypy, ESLint, Prettier, oxlint, and oxfmt through package scripts.

## Testing Guidelines

Pytest discovers `test_*.py` and `*_test.py` files, `Test*` classes, and `test_*` functions under `tests/`. Use markers declared in `pytest.ini`: `unit`, `integration`, `system`, `slow`, and `async`. Prefer targeted commands during development, followed by `pytest` before broader changes. Frontend tests are explicit npm scripts such as `npm run test:tool-result-lifecycle` from `jiuwenswarm/channels/web/frontend/`.

## Commit & Pull Request Guidelines

Recent history uses concise conventional-style commits, commonly `fix(scope): message`, `fix: message`, and occasional bilingual Chinese descriptions. Prefer an imperative summary, for example `fix(tui): show correct Ctrl+D exit message`. Pull requests should describe the change, list validation commands, link the issue or task, and include screenshots or recordings for UI changes.

## Security & Configuration Tips

Do not commit local credentials, model API keys, generated runtime state, or `node_modules/`. Use templates in `jiuwenswarm/resources/` for configuration examples, and keep dependency floors in `pyproject.toml` when they document security fixes.
