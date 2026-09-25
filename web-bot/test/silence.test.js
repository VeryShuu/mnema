// Testy chistogo detektora tişiny (silence.js). Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { isSoundLevel, decideSilence } from "../src/silence.js";

const MIN = 60 * 1000;
const GRACE = 30 * MIN;
const SILENCE = 5 * MIN;
const base = (over) => ({
  nowMs: 1_000_000 + GRACE + SILENCE, joinedAtMs: 1_000_000,
  lastSoundTsMs: 1_000_000, levelDb: -70, thresholdDb: -50,
  graceMs: GRACE, silenceMs: SILENCE, ...over,
});

test("zvuk vyshe poroga — isSoundLevel true", () => {
  assert.equal(isSoundLevel(-30, -50), true);
});

test("zvok rovno porogu i nishe — not zvuk", () => {
  assert.equal(isSoundLevel(-50, -50), false);
  assert.equal(isSoundLevel(-70, -50), false);
});

test("null/undefined/NaN uroven' — ne zvuk", () => {
  assert.equal(isSoundLevel(null, -50), false);
  assert.equal(isSoundLevel(undefined, -50), false);
  assert.equal(isSoundLevel(NaN, -50), false);
});

test("vo vremya greyysa (zvuk est') — ni leave, ni graceDone", () => {
  const d = decideSilence(base({
    nowMs: 1_000_000 + 10 * MIN, lastSoundTsMs: 1_000_000 + 9 * MIN, levelDb: -20,
  }));
  assert.equal(d.graceDone, false);
  assert.equal(d.leave, false);
});

test("greyys 30 min + tişina 5 min podryad — leave", () => {
  const joined = 1_000_000;
  const d = decideSilence(base({
    nowMs: joined + GRACE + SILENCE, joinedAtMs: joined,
    lastSoundTsMs: joined + GRACE, levelDb: -80,
  }));
  assert.equal(d.leave, true);
  assert.ok(Math.abs(d.silenceMin - 5) < 1e-9);
});

test("zvuk sbrasyvaet taymer: leave false i lastSoundTs = now", () => {
  const joined = 1_000_000;
  const now = joined + GRACE + SILENCE;
  const d = decideSilence(base({ nowMs: now, joinedAtMs: joined, lastSoundTsMs: joined + GRACE, levelDb: -20 }));
  assert.equal(d.sound, true);
  assert.equal(d.leave, false);
  assert.equal(d.lastSoundTs, now);
});

test("do okonchaniya greyysa vyhoda net, daže pri dlitel'noy tişine", () => {
  const joined = 1_000_000;
  const d = decideSilence(base({
    nowMs: joined + 29 * MIN, joinedAtMs: joined,
    lastSoundTsMs: joined, levelDb: -90,
  }));
  assert.equal(d.graceDone, false);
  assert.equal(d.leave, false);
});

test("tişina nabegayet vo vremya greyysa, porog po greyysu -> leave syrazu", () => {
  const joined = 1_000_000;
  const d = decideSilence(base({
    nowMs: joined + GRACE, joinedAtMs: joined,
    lastSoundTsMs: joined + GRACE - 10 * MIN, levelDb: -90,
  }));
  assert.equal(d.graceDone, true);
  assert.equal(d.leave, true);
});

test("nedostupnyy uroven' (null) zvuchnym nie schitaetsya, no i ne sbrasyvaet", () => {
  const joined = 1_000_000;
  const now = joined + GRACE + SILENCE;
  const d = decideSilence(base({ nowMs: now, joinedAtMs: joined, lastSoundTsMs: joined + GRACE, levelDb: null }));
  assert.equal(d.sound, false);
  assert.equal(d.lastSoundTs, joined + GRACE);
  assert.equal(d.leave, true); // tişina chislitsya ot poslednego podtverzhennogo zvuka
});

test("granichnyy sluchay: rovvno silenceMs — leave", () => {
  const joined = 1_000_000;
  const d = decideSilence(base({ nowMs: joined + GRACE + SILENCE, lastSoundTsMs: joined + GRACE }));
  assert.equal(d.leave, true);
});

test("na sekundu ranshe poroga — ne leave", () => {
  const joined = 1_000_000;
  const d = decideSilence(base({ nowMs: joined + GRACE + SILENCE - 1, lastSoundTsMs: joined + GRACE }));
  assert.equal(d.leave, false);
});

// --- endAtMs: aktivaciya proverki tishiny ot predpolagaemogo okonchaniya ---

test("endAt: do okonchaniya proverka ne aktivna, daže posle greyysa", () => {
  const joined = 1_000_000;
  const endAt = joined + 2 * GRACE;
  const d = decideSilence(base({
    nowMs: joined + GRACE + SILENCE, joinedAtMs: joined, endAtMs: endAt,
    lastSoundTsMs: joined, levelDb: -90,
  }));
  assert.equal(d.graceDone, false);
  assert.equal(d.leave, false);
});

test("endAt: posle okonchaniya i tişine >= silenceMs — leave", () => {
  const joined = 1_000_000;
  const endAt = joined + 10 * MIN;
  const d = decideSilence(base({
    nowMs: endAt + SILENCE, joinedAtMs: joined, endAtMs: endAt,
    lastSoundTsMs: endAt, levelDb: -90,
  }));
  assert.equal(d.graceDone, true);
  assert.equal(d.leave, true);
});

test("endAt: srazu posle okonchaniya, no tişine masih'e poroga — ne leave", () => {
  const joined = 1_000_000;
  const endAt = joined + 10 * MIN;
  const d = decideSilence(base({
    nowMs: endAt + MIN, joinedAtMs: joined, endAtMs: endAt,
    lastSoundTsMs: endAt, levelDb: -90,
  }));
  assert.equal(d.graceDone, true);
  assert.equal(d.leave, false);
});

test("endAt: endAt ran'şe greyysa — proverka aktivna s endAt (greyys nie tyanetsya)", () => {
  const joined = 1_000_000;
  const endAt = joined + 5 * MIN; // men'şe GRACE
  const d = decideSilence(base({
    nowMs: endAt + SILENCE, joinedAtMs: joined, endAtMs: endAt,
    lastSoundTsMs: endAt, levelDb: -90,
  }));
  assert.equal(d.leave, true);
});

test("endAt null/undefined — padenie na grace-rezhim (obratnaya sovmestimost')", () => {
  const joined = 1_000_000;
  const on = decideSilence(base({
    nowMs: joined + GRACE + SILENCE, joinedAtMs: joined, endAtMs: null,
    lastSoundTsMs: joined + GRACE,
  }));
  assert.equal(on.leave, true);
  const early = decideSilence(base({
    nowMs: joined + 10 * MIN, joinedAtMs: joined, endAtMs: undefined,
    lastSoundTsMs: joined,
  }));
  assert.equal(early.leave, false);
});
