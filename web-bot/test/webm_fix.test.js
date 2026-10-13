// Testy ffmpeg-helpers remontu (webm_fix.js) na real'nom ffmpeg.
// Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { decodedAudioSeconds, reencodeWebm } from "../src/webm_fix.js";

const hasFfmpeg = spawnSync("ffmpeg", ["-version"]).status === 0;

function streamedWebm(dir, seconds = 6) {
  // webm bez cues/duration (kak u Chrome MediaRecorder): probe=N/A, dekod zhivoy
  const r = spawnSync("ffmpeg", ["-v", "error", "-f", "lavfi",
    `-i`, `sine=frequency=440:duration=${seconds}`,
    "-c:a", "libopus", "-b:a", "64k", "-ar", "48000", "-f", "webm", "pipe:1"]);
  const p = path.join(dir, `s${seconds}.webm`);
  fs.writeFileSync(p, r.stdout);
  return p;
}

test("decodedAudioSeconds: streamed webm bez probe-metadannykh schityvaetsya",
  { skip: !hasFfmpeg && "nuzhen ffmpeg" }, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "webmfix-"));
    const p = streamedWebm(dir, 6);
    const d = decodedAudioSeconds(p);
    assert.ok(d >= 4.5 && d <= 7.5, `dekod ${d} s vshe 6 s`);
  });

test("decodedAudioSeconds: garbage i nesushchestvuyushchiy fayl dayut 0",
  { skip: !hasFfmpeg && "nuzhen ffmpeg" }, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "webmfix-"));
    const bad = path.join(dir, "bad.webm");
    fs.writeFileSync(bad, Buffer.concat(Array(50).fill(Buffer.from("garbage!!"))));
    assert.equal(decodedAudioSeconds(bad), 0);
    assert.equal(decodedAudioSeconds(path.join(dir, "net.webm")), 0);
  });

test("reencodeWebm: iz postradavshego konteynera delayet chisty s probe",
  { skip: !hasFfmpeg && "nuzhen ffmpeg" }, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "webmfix-"));
    const src = streamedWebm(dir, 6);
    const dst = path.join(dir, "fixed.webm");
    assert.equal(reencodeWebm(src, dst), true);
    const probe = spawnSync("ffprobe", ["-v", "error", "-show_entries",
      "format=duration", "-of", "default=nw=1:nk=1", dst], { encoding: "utf8" });
    const dur = parseFloat(probe.stdout);
    assert.ok(dur >= 4.5 && dur <= 7.5, `probe posle reencode: ${dur}`);
  });

test("reencodeWebm: garbage — False bez sozdaniya vykhoda",
  { skip: !hasFfmpeg && "nuzhen ffmpeg" }, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "webmfix-"));
    const bad = path.join(dir, "bad.webm");
    fs.writeFileSync(bad, Buffer.concat(Array(50).fill(Buffer.from("garbage!!"))));
    const dst = path.join(dir, "fixed.webm");
    assert.equal(reencodeWebm(bad, dst), false);
  });
