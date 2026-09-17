import assert from "node:assert/strict";

import { CommandService } from "../dist/core/commands/CommandService.js";
import {
  createEvolveCommand,
  createEvolveListCommand,
  createEvolveRebuildCommand,
  createEvolveSimplifyCommand,
} from "../dist/core/commands/builtins/evolve.js";

const commands = [
  createEvolveCommand(),
  createEvolveListCommand(),
  createEvolveRebuildCommand(),
  createEvolveSimplifyCommand(),
];
const commandNames = commands.map((command) => command.name);
const service = new CommandService();
service.register(commands);

// The frontend must fail closed until config.get explicitly enables evolution.
assert.deepEqual(service.getAll().map((command) => command.name), []);
assert.deepEqual(
  service.getAll(true).map((command) => command.name).sort(),
  [...commandNames].sort(),
);
assert.equal(service.setSkillEvolutionEnabled(true), true);
assert.deepEqual(service.getAll().map((command) => command.name).sort(), [...commandNames].sort());
assert.equal(service.setSkillEvolutionEnabled(false), true);
assert.deepEqual(service.getAll().map((command) => command.name), []);

// Hidden commands remain executable when typed explicitly; the backend owns the
// final disabled error and receives the original slash request.
const sent = [];
await service.execute("/evolve pptx", {
  mode: "agent.plan",
  sessionId: "test-session",
  sendMessage: (content) => {
    sent.push(content);
    return "request-1";
  },
  addItem: () => {},
});
assert.deepEqual(sent, ["/evolve pptx"]);

console.log("evolution-command-gate tests passed");
