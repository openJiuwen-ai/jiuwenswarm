import assert from "node:assert/strict";
import test from "node:test";

import {
  hasXiaoyiPushApiId,
  isCronTargetOptionDisabled,
} from "../node_modules/.cache/xiaoyi-cron-target/components/CronPanel/xiaoyiCronTarget.js";

test("hasXiaoyiPushApiId is false for empty/missing conf", () => {
  assert.equal(hasXiaoyiPushApiId(undefined), false);
  assert.equal(hasXiaoyiPushApiId(null), false);
  assert.equal(hasXiaoyiPushApiId({}), false);
  assert.equal(hasXiaoyiPushApiId({ apps: [] }), false);
});

test("hasXiaoyiPushApiId accepts flat conf with non-empty api_id", () => {
  assert.equal(hasXiaoyiPushApiId({ api_id: "webhook-1" }), true);
  assert.equal(hasXiaoyiPushApiId({ api_id: "  " }), false);
  assert.equal(hasXiaoyiPushApiId({ api_id: "webhook-1", enabled: false }), false);
});

test("hasXiaoyiPushApiId requires enabled app with non-empty api_id", () => {
  assert.equal(
    hasXiaoyiPushApiId({
      apps: [{ enabled: true, api_id: "" }],
    }),
    false,
  );
  assert.equal(
    hasXiaoyiPushApiId({
      apps: [{ enabled: true, api_id: "  webhook43  " }],
    }),
    true,
  );
  assert.equal(
    hasXiaoyiPushApiId({
      apps: [
        { enabled: false, api_id: "disabled-has-id" },
        { enabled: true, api_id: "" },
      ],
    }),
    false,
  );
  assert.equal(
    hasXiaoyiPushApiId({
      apps: [
        { enabled: false, api_id: "disabled-has-id" },
        { enabled: true, api_id: "ready" },
      ],
    }),
    true,
  );
});

test("isCronTargetOptionDisabled keeps non-xiaoyi channels on enabled-only rule", () => {
  const enabled = new Set(["web", "xiaoyi", "feishu"]);
  assert.equal(isCronTargetOptionDisabled("web", enabled, false), false);
  assert.equal(isCronTargetOptionDisabled("feishu", enabled, true), false);
  assert.equal(isCronTargetOptionDisabled("dingtalk", enabled, true), true);
});

test("isCronTargetOptionDisabled greys out xiaoyi without api_id even when registered", () => {
  const enabled = new Set(["web", "xiaoyi"]);
  assert.equal(isCronTargetOptionDisabled("xiaoyi", enabled, false), true);
  assert.equal(isCronTargetOptionDisabled("xiaoyi", enabled, true), false);
  assert.equal(isCronTargetOptionDisabled("xiaoyi", new Set(["web"]), true), true);
});
