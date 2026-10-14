// Testy edinogo loggera (log.js). Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const LOG_JS = fileURLToPath(new URL("../src/log.js", import.meta.url));

const SNIFF = `
import { createLogger } from ${JSON.stringify(LOG_JS
  .split(path.sep).join("/"))};
const log = createLogger("test");
log.debug("d-line");
log.info("i-line");
log.warn("w-line");
log.error("e-line line\\nsecond");
`;

function run(env = {}) {
  const r = spawnSync(process.execPath, ["--input-type=module", "-e", SNIFF], {
    encoding: "utf8", env: { ...process.env, LOG_LEVEL: "", ...env },
  });
  return { out: r.stdout, err: r.stderr };
}

test("default level info: debug hidden, info/warn/error shown", () => {
  const { out, err } = run();
  assert.match(out, /INFO \[test\] i-line/);
  assert.match(err, /WARN \[test\] w-line/);
  assert.match(err, /ERROR \[test\] e-line/);
  assert.doesNotMatch(out + err, /d-line/);
});

test("LOG_LEVEL=debug shows debug too", () => {
  const { out } = run({ LOG_LEVEL: "debug" });
  assert.match(out, /DEBUG \[test\] d-line/);
});

test("LOG_LEVEL=error hides info and warn", () => {
  const { out, err } = run({ LOG_LEVEL: "error" });
  assert.doesNotMatch(out + err, /i-line|w-line/);
  assert.match(err, /ERROR \[test\] e-line/);
});

test("line format: ISO timestamp, level, [component]", () => {
  const { out } = run();
  assert.match(out, /^\d{4}-\d{2}-\d{2}T[\d:.]+Z INFO \[test\] i-line\n/);
});
