// Testy zashchity zapisi ot poteri audio (record_guard.js): idempotentnaya
// dopiska chankov, revive payplayna, imena syrykh segmentov.
// Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { chunkWritePlan, needsRevive, parseRawSegName } from "../src/record_guard.js";

test("pervyy chunk v pusty fayl: pishem s nachala bez nulov", () => {
  assert.deepEqual(chunkWritePlan(0, 0, 10), { zeros: 0, sliceFrom: 0 });
});

test("povtornyj chunk uze na diskе — null (nicego pishut nie nuzhno)", () => {
  assert.equal(chunkWritePlan(10, 0, 10), null);
});

test("chastichnaya predydushchaya zapis': otsekajem head, nullov niet", () => {
  // ENOSPC posle 5 bajt: pri povtore dolzhna zapisat'sya tol'ko hvost
  assert.deepEqual(chunkWritePlan(5, 0, 10), { zeros: 0, sliceFrom: 5 });
});

test("propusk mezhdu zapisyami — zalivka nulami pered chunk", () => {
  assert.deepEqual(chunkWritePlan(10, 15, 10), { zeros: 5, sliceFrom: 0 });
});

test("propusk s overlap — nulli do granicy i otsechennyy head", () => {
  assert.deepEqual(chunkWritePlan(12, 10, 10), { zeros: 0, sliceFrom: 2 });
});

test("nekorektnye vkhodnye — null (gap nul'dl. i otricatel'nyy offset)", () => {
  assert.equal(chunkWritePlan(0, 0, 0), null);
  assert.equal(chunkWritePlan(0, 0, -5), null);
  assert.equal(chunkWritePlan(0, -1, 10), null);
  assert.equal(chunkWritePlan(0, 0, NaN), null);
});

test("zdorovyy payplayn (recording+running) — revive nie nuzhen", () => {
  assert.equal(needsRevive({ armed: true, win: 1, recording: true, ctxState: "running" }), false);
});

test("MediaRecorder umer pri vooruzhennom okne — revive nuzhen", () => {
  assert.equal(needsRevive({ armed: true, win: 1, recording: false, ctxState: "running" }), true);
});

test("AudioContext zas — revive nuzhen (i pri recording=true)", () => {
  assert.equal(needsRevive({ armed: true, win: 2, recording: true, ctxState: "suspended" }), true);
  assert.equal(needsRevive({ armed: true, win: 2, recording: false, ctxState: "suspended" }), true);
});

test("nie vooruzheno i nie okna — revive nie nuzhen (pre-arm)", () => {
  assert.equal(needsRevive({ armed: false, win: 0, recording: false, ctxState: null }), false);
  assert.equal(needsRevive({ armed: true, win: 0, recording: false, ctxState: "running" }), false);
});

test("closed AudioContext v etom zhe kontekste ne razбудut'sya — ne lovim", () => {
  assert.equal(needsRevive({ armed: true, win: 1, recording: false, ctxState: "closed" }), false);
});

test("imya syrogo segmenta razbiraetsya, proheye — net", () => {
  assert.deepEqual(parseRawSegName("raw.3_w2.webm"), { n: 3, w: 2 });
  assert.deepEqual(parseRawSegName("raw.0_w1.webm"), { n: 0, w: 1 });
  assert.equal(parseRawSegName("raw.webm"), null);
  assert.equal(parseRawSegName("raw.0_w1.webm.p0.webm"), null);
  assert.equal(parseRawSegName("meeting.wav"), null);
});
