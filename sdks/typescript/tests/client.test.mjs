// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { mkdtemp, readFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Client, ProtocolError, InteractionRequired } from "../dist/index.js";

const fixture = fileURLToPath(
  new URL("../../tests/fixture_child.py", import.meta.url),
);
const python = process.env.SDK_TEST_PYTHON ?? "python";
const client = (mode = "success", options = {}) =>
  new Client({
    command: [python, fixture],
    env: { SDK_FIXTURE: mode },
    shutdownGraceMs: 1000,
    ...options,
  });

for (const mode of ["success", "split_utf8", "stderr"])
  test(`real pipes ${mode}`, async () => {
    const result = await client(mode).run(
      { input: "test" },
      { deadlineMs: 10000 },
    );
    assert.equal(result.exit_code, 0);
    assert.equal(result.output, "hello 中文 😀");
  });

test("each query owns a different process", async () => {
  const sdk = client();
  const first = await sdk.query("mode.list");
  const second = await sdk.query("model.list");
  assert.notEqual(first.data.pid, second.data.pid);
  assert.equal(first.session_id, null);
});

for (const mode of [
  "bad_id",
  "bad_version",
  "bad_sequence",
  "partial",
  "bad_utf8",
  "empty",
  "extra",
  "wrong_exit",
  "duplicate",
  "overflow_number",
])
  test(`reject ${mode}`, async () => {
    await assert.rejects(
      client(mode).run({ input: "test" }, { deadlineMs: 10000 }),
      ProtocolError,
    );
  });

test("bounded output", async () => {
  await assert.rejects(
    client("oversize", { maxRecordBytes: 4096 }).run({ input: "test" }),
    ProtocolError,
  );
});
test("failed result is returned without retry", async () => {
  assert.equal((await client("error").run({ input: "test" })).exit_code, 1);
});
test("two interactions share one child", async () => {
  const seen = [];
  const result = await client("two_questions").run(
    { input: "test" },
    {
      onInteraction(event) {
        seen.push(event.payload.interaction_id);
        return [
          { question: "answer", selected_options: ["yes"], custom_input: "" },
        ];
      },
      deadlineMs: 10000,
    },
  );
  assert.equal(result.exit_code, 0);
  assert.deepEqual(seen, ["opaque-0", "opaque-1"]);
});
test("no automatic approval", async () => {
  await assert.rejects(
    client("ask").run({ input: "test" }),
    InteractionRequired,
  );
});
test("cancel stops actual child before returning", async () => {
  const directory = await mkdtemp(join(tmpdir(), "jiuwen-sdk-test-"));
  const marker = join(directory, "closed");
  const control = new AbortController();
  const sdk = client("wait", {
    env: { SDK_FIXTURE: "wait", SDK_MARKER: marker },
  });
  const result = await sdk.run(
    { input: "test" },
    {
      signal: control.signal,
      onEvent() {
        control.abort();
      },
      deadlineMs: 10000,
    },
  );
  assert.equal(result.exit_code, 130);
  assert.ok(await readFile(marker, "utf8"));
});
test("host callback failure cancels", async () => {
  const directory = await mkdtemp(join(tmpdir(), "jiuwen-sdk-test-"));
  const marker = join(directory, "closed");
  const sdk = client("ask", {
    env: { SDK_FIXTURE: "ask", SDK_MARKER: marker },
  });
  await assert.rejects(
    sdk.run(
      { input: "test" },
      {
        onInteraction() {
          throw new Error("host callback");
        },
      },
    ),
    /host callback/,
  );
  assert.ok(await readFile(marker, "utf8"));
});
test("deadline bounds pending host callback", async () => {
  const directory = await mkdtemp(join(tmpdir(), "jiuwen-sdk-test-"));
  const marker = join(directory, "closed");
  const sdk = client("wait", {
    env: { SDK_FIXTURE: "wait", SDK_MARKER: marker },
  });
  await assert.rejects(
    sdk.run(
      { input: "test" },
      { onInteraction: () => new Promise(() => {}), deadlineMs: 300 },
    ),
    /deadline/,
  );
  assert.ok(await readFile(marker, "utf8"));
});
test("result alone is insufficient; wait for process exit", async () => {
  const directory = await mkdtemp(join(tmpdir(), "jiuwen-sdk-test-"));
  const marker = join(directory, "closed");
  const result = await client("delayed_exit", {
    env: { SDK_FIXTURE: "delayed_exit", SDK_MARKER: marker },
  }).run({ input: "test" });
  assert.equal(result.exit_code, 0);
  assert.ok(await readFile(marker, "utf8"));
});
test("spawn error cannot be success", async () => {
  await assert.rejects(
    new Client({ command: ["nonexistent-jiuwen-sdk-program"] }).run({
      input: "test",
    }),
  );
});

test("cancellation need not wait for an unanswered host dialog", async () => {
  const controller = new AbortController();
  const result = await client("wait").run(
    { input: "test" },
    {
      signal: controller.signal,
      deadlineMs: 2000,
      onInteraction() {
        controller.abort();
        return new Promise(() => {});
      },
    },
  );
  assert.equal(result.exit_code, 130);
});

test("force fallback collects the owned child tree", async () => {
  const directory = await mkdtemp(join(tmpdir(), "jiuwen-sdk-test-"));
  const marker = join(directory, "owned-pids");
  const sdk = client("stubborn", {
    env: { SDK_FIXTURE: "stubborn", SDK_MARKER: marker },
    shutdownGraceMs: 200,
  });
  await assert.rejects(
    sdk.run({ input: "test" }, { deadlineMs: 500 }),
    /deadline/,
  );
  const pids = JSON.parse(await readFile(marker, "utf8"));
  const running = (pid) => {
    try {
      process.kill(pid, 0);
      if (process.platform === "linux")
        return (
          readFileSync(`/proc/${pid}/stat`, "utf8")
            .split(")")
            .at(-1)
            .trim()
            .split(" ")[0] !== "Z"
        );
      return true;
    } catch {
      return false;
    }
  };
  for (let i = 0; i < 20 && pids.some(running); i++)
    await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(pids.some(running), false);
});
