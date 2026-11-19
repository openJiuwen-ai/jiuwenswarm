import assert from "node:assert/strict";
import test from "node:test";

import {
  isCronTargetOptionDisabled,
  isCronTargetSupported,
} from "../node_modules/.cache/xiaoyi-cron-target/xiaoyiCronTarget.mjs";

test("new scheduled tasks do not offer xiaoyi", () => {
  assert.equal(isCronTargetSupported("xiaoyi"), false);
  assert.equal(isCronTargetSupported("xiaoyi:app"), false);
  assert.equal(isCronTargetSupported("web"), true);
});

test("isCronTargetOptionDisabled only requires an enabled channel", () => {
  const enabled = new Set(["web", "feishu"]);
  assert.equal(isCronTargetOptionDisabled("web", enabled), false);
  assert.equal(isCronTargetOptionDisabled("feishu", enabled), false);
  assert.equal(isCronTargetOptionDisabled("dingtalk", enabled), true);
  assert.equal(isCronTargetOptionDisabled("xiaoyi", new Set(["xiaoyi", "web"])), true);
});
