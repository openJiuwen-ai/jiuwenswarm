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
// Use agent.work.plan (supported) so the frontend forwards the slash request.
const sent = [];
const evolveEntries = [];
await service.execute("/evolve pptx", {
  mode: "agent.work.plan",
  sessionId: "test-session",
  sendMessage: (content) => {
    sent.push(content);
    return "request-1";
  },
  addItem: (item) => evolveEntries.push(item),
});
assert.deepEqual(sent, ["/evolve pptx"]);
assert.equal(evolveEntries.length, 0);

// Unsupported modes surface a frontend error instead of forwarding.
const sentUnsupported = [];
const unsupportedEntries = [];
await service.execute("/evolve pptx", {
  mode: "agent.work.normal",
  sessionId: "test-session-2",
  sendMessage: (content) => {
    sentUnsupported.push(content);
    return "request-2";
  },
  addItem: (item) => unsupportedEntries.push(item),
});
assert.deepEqual(sentUnsupported, []);
assert.equal(unsupportedEntries.length, 1);
assert.match(unsupportedEntries[0].content, /演进功能不可用/);

// 回归：team.work.normal / team.code.normal 必须被放行,原集合 {team, code.team}
// 在新 canonical 下覆盖这两个 normal 变体,重构时漏列会误拒（回归 #1）。
for (const teamNormalMode of ["team.work.normal", "team.code.normal"]) {
  const sentTeam = [];
  const teamEntries = [];
  await service.execute("/evolve pptx", {
    mode: teamNormalMode,
    sessionId: `test-session-team-${teamNormalMode}`,
    sendMessage: (content) => {
      sentTeam.push(content);
      return `req-${teamNormalMode}`;
    },
    addItem: (item) => teamEntries.push(item),
  });
  assert.deepEqual(sentTeam, ["/evolve pptx"], `${teamNormalMode} should be supported`);
  assert.equal(teamEntries.length, 0, `${teamNormalMode} must not surface error`);
}

console.log("evolution-command-gate tests passed");
