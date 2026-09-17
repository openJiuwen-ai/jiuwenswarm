process.stdin.once("data", (data) => {
  process.stdout.write(`EDITOR_INPUT:${data.toString("utf8").trim()}\n`);
  process.exit(0);
});
process.stdin.resume();
