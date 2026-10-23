// Testy salvage.js: sborka final'noj zapisi Po syrym segmentam na diske
// (scenarii ubitogo/ restartovan'nogo web-bot). Nuzhny ffmpeg/ffprobe.
// Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { execFileSync } from "node:child_process";
import { rawSegsFromDisk, salvageFromDisk } from "../src/salvage.js";

function haveFfmpeg() {
  try { execFileSync("ffmpeg", ["-version"], { stdio: "ignore" }); return true; }
  catch { return false; }
}

// webm kakMediaRecorder: bez cues/duration (streamed), chtoby provit'
// celyy put' finalize s validaciey.
function streamedWebm(p, seconds) {
  const data = execFileSync("ffmpeg", ["-v", "error", "-f", "lavfi",
    "-i", `sine=frequency=440:duration=${seconds}`, "-c:a", "libopus",
    "-b:a", "64k", "-ar", "48000", "-f", "webm", "pipe:1"]);
  fs.writeFileSync(p, data);
  return p;
}

test("rawSegsFromDisk: poryadok po n, epoch po bajtam, prochie fayly ignor", async () => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "salv-"));
  // <256b segmenty filterUsableSegs otbrasyvaet — delay >1KiB kazhdomu
  fs.writeFileSync(path.join(d, "raw.0_w1.webm"), Buffer.alloc(8000));
  fs.writeFileSync(path.join(d, "raw.2_w2.webm"), Buffer.alloc(16000));
  fs.writeFileSync(path.join(d, "raw.1_w2.webm.p0.webm"), Buffer.alloc(100));
  fs.writeFileSync(path.join(d, "meeting.wav"), Buffer.alloc(50));
  fs.writeFileSync(path.join(d, "raw.webm"), Buffer.alloc(999));
  const segs = rawSegsFromDisk(d);
  assert.deepEqual(segs.map((s) => s.n), [0, 2], "tol'ko syryye segmenty bez razrezki/rezulta");
  assert.equal(segs[0].bytes, 8000);
  assert.equal(segs[0].epochMs, 0);
  assert.equal(segs[1].epochMs, Math.round(8000 / 8), "otsenka epoh: +bytes/8 ms");
});

test("rawSegsFromDisk: pustoy/otsutstvuyushchiy katalog — [], ne padenie", async () => {
  assert.deepEqual(rawSegsFromDisk("/nonexistent/xyz"), []);
});

test("salvage: uze finalize (raw.webm est') — otdaj puti, ne peresobiraj", async () => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "salv-"));
  const jobDir = path.join(d, "jobs", "77");
  fs.mkdirSync(jobDir, { recursive: true });
  fs.writeFileSync(path.join(jobDir, "raw.webm"), Buffer.alloc(5000));
  fs.writeFileSync(path.join(jobDir, "raw.0_w1.webm"), Buffer.alloc(5000));
  const cfg = { dataDir: d };
  const res = await salvageFromDisk(77, cfg);
  assert.equal(res.ok, true);
  assert.equal(res.already_finalized, true);
  assert.equal(res.end_reason, "already_finalized");
  assert.equal(res.sizes.webm, 5000);
});

test("salvage: syryye bez final'nyh i nie > MIN_SEGMENT_BYTES — no_raw_segments", async () => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "salv-"));
  const jobDir = path.join(d, "jobs", "78");
  fs.mkdirSync(jobDir, { recursive: true });
  fs.writeFileSync(path.join(jobDir, "raw.0_w1.webm"), Buffer.alloc(10));
  const res = await salvageFromDisk(78, { dataDir: d });
  assert.equal(res.ok, false);
  assert.equal(res.fail_reason, "no_raw_segments");
});

test("salvage: syryye segmenty -> finalize sozdaet raw.webm/meeting.mp3", { skip: !haveFfmpeg() }, async () => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "salv-"));
  const jobDir = path.join(d, "jobs", "42");
  fs.mkdirSync(jobDir, { recursive: true });
  streamedWebm(path.join(jobDir, "raw.0_w1.webm"), 4);
  const res = await salvageFromDisk(42, { dataDir: d });
  assert.equal(res.ok, true, `salvage dayet ok; fail=${res.fail_reason}`);
  assert.ok(fs.existsSync(path.join(jobDir, "raw.webm")));
  assert.ok(res.duration_s >= 2, `dlitelnost' iz dekada: ${res.duration_s}`);
  assert.ok(res.sizes.webm > 1000);
});

test("salvage: pusty katalog job — no_raw_segments", async () => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "salv-"));
  fs.mkdirSync(path.join(d, "jobs", "79"), { recursive: true });
  const res = await salvageFromDisk(79, { dataDir: d });
  assert.equal(res.ok, false);
  assert.equal(res.fail_reason, "no_raw_segments");
});

test("salvage: itog-tishina pri bol'shih syryh fragmentah — suspect, syryye SOHRANYAYUTSYA (regressiya job 24)", { skip: !haveFfmpeg() }, async () => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "salv-"));
  const jobDir = path.join(d, "jobs", "24");
  fs.mkdirSync(jobDir, { recursive: true });
  // molchal'nyy chitaemyy segmet + big neschitaemyy fragment s real'nym audio
  streamedWebm(path.join(jobDir, "raw.0_w1.webm"), 2);
  fs.writeFileSync(path.join(jobDir, "raw.1_w1.webm"), Buffer.alloc(2_000_000, 0x41));
  const events = [];
  const res = await salvageFromDisk(24, { dataDir: d }, (e, det) => events.push([e, det]));
  assert.equal(res.ok, true);
  assert.ok(fs.existsSync(path.join(jobDir, "raw.webm")), "final'nyy webm na meste");
  assert.ok(fs.existsSync(path.join(jobDir, "raw.1_w1.webm")),
    "podozritel'nyy syroys segment ne udalen");
  assert.ok(fs.existsSync(path.join(jobDir, "raw.0_w1.webm")),
    "molchal'nyy syroys segment ne udalen");
  assert.ok(events.some(([e, det]) => e === "error" && /под вопросом/.test(det)),
    `events: ${JSON.stringify(events)}`);
});

test("salvage: dva chitaemyh segmenta — mikš zhiv, syryye udalyayutsya", { skip: !haveFfmpeg() }, async () => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "salv-"));
  const jobDir = path.join(d, "jobs", "43");
  fs.mkdirSync(jobDir, { recursive: true });
  streamedWebm(path.join(jobDir, "raw.0_w1.webm"), 2);
  streamedWebm(path.join(jobDir, "raw.1_w1.webm"), 2);
  const res = await salvageFromDisk(43, { dataDir: d });
  assert.equal(res.ok, true);
  assert.ok(fs.existsSync(path.join(jobDir, "raw.webm")));
  assert.ok(!fs.existsSync(path.join(jobDir, "raw.0_w1.webm")), "syryye posle uspeshnogo miksa udaleny");
});
