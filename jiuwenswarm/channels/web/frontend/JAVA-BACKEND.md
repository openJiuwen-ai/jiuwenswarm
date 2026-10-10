# Java backend development

This shared WebUI is maintained directly on the official `jiuwenswarm-java`
branch. Java adaptations are normal source changes in this repository, not
startup-time patches or a second frontend copy. The Java backend remains in
its own `jiuwenswarm-java` repository. No Python backend is required in Java mode.

## Run the frontend

Use Node 20+ and install with `npm ci` in this directory. After starting Java
AgentServer (18093) and Gateway (18999), run:

```bash
JIUWENSWARM_BACKEND=java WEB_PORT=18999 FRONTEND_PORT=5174 npm run dev
```

Open `http://127.0.0.1:5174` locally, or forward port 5174 over SSH/VS Code.
Directory selection refers to the backend host, not the browser computer.
Never put model API keys in `VITE_*` variables; configure models through the
Gateway settings page or the backend's environment. Both Java services must
share the same data directory. Persisted model selection takes precedence.

From the separate Java repository, start all three processes with:

```bash
bash scripts/run-webui-linux.sh --python-root /path/to/this-repository \
  --data-dir /path/to/java-data
```

`--python-root` means the repository root (not this frontend directory).
The Java launcher recognizes `java-backend.json` and validates the integrated
contract without applying or reverting source patches. Ordinary commits and
local UI edits do not require changing a pinned Git SHA. The compatibility
declaration does not certify behavior: run the tests below before delivery.

## Maintain and verify

The initial integration uses commit `483f2be45172712c6650000538bfc879dc5091c0`.
Its frontend matches the validated v0.2.7 source at
`2aa7fea7e55fbe1edaae0d468ef666b088c76fa9`. This does not advance the Java
backend's Python 0.2.5 behavior migration baseline.

- Keep Java-specific behavior behind `JIUWENSWARM_BACKEND=java`. Without it,
  Python startup, workspace generation and filesystem middleware remain intact.
- Java mode sends `/api`, `/ws`, `/ws/git` and `/file-api` to Gateway; it disables
  Vite filesystem handlers and workspace generators. Authorization belongs to Java.
- `/api/capabilities` controls unsupported entries; do not advertise absent services.
- Preserve model import/save/default selection, server directory selection,
  permissions, team snapshot/history recovery and Code diffs.
- Update the contract version only for intentional boundary changes coordinated
  with the Java consumer. Do not reapply the old exported v0.2.7 patch here.
- Submit shared frontend changes in this repository; submit Java changes and
  cross-stack browser tests in the Java repository. Do not copy UI sources back.

```bash
npm run test:java-backend
JIUWENSWARM_BACKEND=java npm run build
git diff --check
# From the Java repository (local fake model; no real credentials):
bash frontend/web/compat-tests/run-browser-acceptance.sh --python-root /path/to/this-repository
```

The browser gate verifies model persistence, streaming, reasoning, cancellation,
file tools/approval, Code diffs, Team execution/recovery and search error states.
Real cloud-model, search and Ascend KV-cache service checks are separate online
acceptance; passing local fixtures must not be represented as those services passing.

On the initial integration, Java-mode build and the 15-case cross-stack browser
suite plus restart recovery pass. The upstream Settings suite still has three
pre-existing failures (ASR fields, multimodal controls, browser locale keys),
and the reasoning DOM suite has five (after its missing Vite environment is
supplied). These were reproduced against upstream business sources and were
not disabled. Do not describe all upstream frontend tests as passing.
