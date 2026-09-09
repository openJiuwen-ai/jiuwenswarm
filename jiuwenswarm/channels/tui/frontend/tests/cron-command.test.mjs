import assert from "node:assert/strict";

import { createCronCommand } from "../dist/core/commands/builtins/cron.js";

function makeContext() {
  const items = [];
  const requests = [];
  return {
    items,
    requests,
    ctx: {
      sessionId: "cron-test",
      addItem: (item) => items.push(item),
      request: async (method, params) => {
        requests.push({ method, params });
        if (method === "cron.job.meta") {
          return { modes: ["agent.work.normal"], default_mode: "agent.work.normal" };
        }
        if (method === "cron.job.create") {
          return { job: { id: "job-1", enabled: true, expired: false, ...params } };
        }
        throw new Error(`unexpected request: ${method}`);
      },
    },
  };
}

const command = createCronCommand();

// croniter's DOW numbering is 0=Sunday through 6=Saturday.  The TUI must not
// reject Sunday before the backend sees the expression.
{
  const { ctx, items, requests } = makeContext();
  await command.action(ctx, 'add name=sunday cron_expr="0 0 9 ? * 0 *" description="weekly reminder"');
  const create = requests.find((request) => request.method === "cron.job.create");
  assert.equal(create?.params.cron_expr, "0 0 9 ? * 0 *");
  assert.equal(items.at(-1)?.kind, "info");
}

// 7 is not a croniter DOW value and must be rejected locally in both formats.
for (const cronExpr of ["0 0 9 ? * 7 *", "0 9 * * 7"]) {
  const { ctx, items, requests } = makeContext();
  await command.action(ctx, `add name=invalid cron_expr="${cronExpr}" description="weekly reminder"`);
  assert.equal(requests.length, 0);
  assert.equal(items.length, 1);
  assert.equal(items[0].kind, "error");
  assert.match(items[0].content, /dow\(0-6; 0=Sun\) 字段无效/);
}

console.log("cron-command tests passed");
