import assert from "node:assert/strict";

import { workflowStatusIcon } from "../dist/core/workflows.js";
import { DEFAULT_BINDINGS } from "../dist/core/keybindings/defaultBindings.js";
import {
  KEYBINDING_ACTIONS,
  KEYBINDING_ACTION_DESCRIPTIONS,
} from "../dist/core/keybindings/actions.js";

// 1. WorkflowStatus "paused" — icon must exist and differ from the other statuses.
const pausedIcon = workflowStatusIcon("paused");
assert.equal(typeof pausedIcon, "string");
assert.ok(pausedIcon.length > 0, "paused icon must be non-empty");
const otherStatuses = [
  "planned",
  "pending",
  "running",
  "completed",
  "failed",
  "stopped",
  "waiting_for_human",
];
assert.equal(
  otherStatuses.map((status) => workflowStatusIcon(status)).includes(pausedIcon),
  false,
  "paused icon must differ from other statuses",
);

// 2. SwarmWorkflows default bindings expose the control keys.
const swarmBindings =
  DEFAULT_BINDINGS.find((block) => block.context === "SwarmWorkflows")?.bindings ?? {};
assert.equal(swarmBindings["shift+p"], "swarm:pauseResume");
assert.equal(swarmBindings["shift+s"], "swarm:stop");

// 3. The actions list and i18n description map cover the new control actions.
assert.ok(KEYBINDING_ACTIONS.includes("swarm:pauseResume"));
assert.ok(KEYBINDING_ACTIONS.includes("swarm:stop"));
for (const action of ["swarm:pauseResume", "swarm:stop"]) {
  const description = KEYBINDING_ACTION_DESCRIPTIONS[action];
  assert.equal(typeof description, "string");
  assert.ok(description.length > 0, `${action} must have a non-empty description`);
}

console.log("swarmflow-control-keys tests passed");
