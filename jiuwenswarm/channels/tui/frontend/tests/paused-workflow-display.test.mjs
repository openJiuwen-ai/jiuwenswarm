import assert from "node:assert/strict";

import {
  formatWorkflowDurationLabel,
  formatWorkflowRunningText,
  pausedWorkflowsBannerText,
  runningWorkflowsBannerText,
  WORKFLOW_STATUS_BANNER,
} from "../dist/core/workflows.js";

// 1. Runtime label — paused is its own label, not "running".
assert.equal(formatWorkflowDurationLabel("paused"), "paused");
assert.equal(formatWorkflowDurationLabel("running"), "running");
assert.equal(formatWorkflowDurationLabel("pending"), "running");

// 2. Running text composes the paused label with elapsed time.
const pausedText = formatWorkflowRunningText({
  id: "w1",
  name: "demo",
  summary: "",
  status: "paused",
  duration_ms: 5000,
  phases: [],
});
assert.ok(
  pausedText.includes("paused"),
  `expected a "paused" label, got: ${JSON.stringify(pausedText)}`,
);
assert.ok(
  !pausedText.includes("running"),
  `paused must not read "running", got: ${JSON.stringify(pausedText)}`,
);

// 3. Detail-page status banner covers paused (and stopped).
assert.equal(WORKFLOW_STATUS_BANNER.paused, "Workflow paused");
assert.equal(WORKFLOW_STATUS_BANNER.stopped, "Workflow stopped");
assert.equal(WORKFLOW_STATUS_BANNER.running, "Workflow running");

// 4. Main-surface banner text for paused workflows.
assert.equal(pausedWorkflowsBannerText(1), "1 workflow paused");
assert.equal(pausedWorkflowsBannerText(2), "2 workflows paused");
assert.equal(pausedWorkflowsBannerText(0), "");
assert.equal(runningWorkflowsBannerText(1), "1 workflow running");

console.log("paused-workflow-display tests passed");
