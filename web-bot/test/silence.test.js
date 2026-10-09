// Testy chistogo detektora tişiny (silence.js). Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { isSoundLevel, decideSilence, shouldAskConfirmEnd } from "../src/silence.js";

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

test("nedostupnyy uroven' (null) zamoraživaet taymer: leave false (job 14)", () => {
  const joined = 1_000_000;
  const now = joined + GRACE + SILENCE;
  const d = decideSilence(base({ nowMs: now, joinedAtMs: joined, lastSoundTsMs: joined + GRACE, levelDb: null }));
  assert.equal(d.sound, false);
  assert.equal(d.lastSoundTs, now); // taymer zamorozhen, a nie prodolzhen
  assert.equal(d.leave, false); // vendostupnoye izmereniye nie golosuyet za tişinu
});

test("regressiya job 14: null-tiki ne navyazyvayut tishinu, srazu posle vosstanovleniya — ne leave", () => {
  const joined = 1_000_000;
  let ts = joined;
  let lastSoundTs = joined;
  // ves'chas analyser nie zhil: null-tiki every 10 s
  for (let i = 0; i < 60 * 6; i++) {
    ts += 10_000;
    const d = decideSilence(base({ nowMs: ts, joinedAtMs: joined, lastSoundTsMs: lastSoundTs, levelDb: null }));
    lastSoundTs = d.lastSoundTs;
    assert.equal(d.leave, false);
  }
  // izmereniye vosstanovilos' i pervyy ze tik — tihiy: tişina ot odnogo tika, ne 60 min
  ts += 10_000;
  const d = decideSilence(base({ nowMs: ts, joinedAtMs: joined, lastSoundTsMs: lastSoundTs, levelDb: -80 }));
  assert.equal(d.leave, false);
  assert.ok(d.silenceMin < 1);
});

test("podryad izmeryonnye tihie tiki posле vosstanovleniya navyazyvayut tishinu — leave", () => {
  const joined = 1_000_000;
  let ts = joined + GRACE;
  let lastSoundTs = ts;
  for (let i = 0; i < 30; i++) { // 30 tikov po 10 s = 5 min tishiny
    ts += 10_000;
    const d = decideSilence(base({ nowMs: ts, joinedAtMs: joined, lastSoundTsMs: lastSoundTs, levelDb: -80 }));
    lastSoundTs = d.lastSoundTs;
    if (i < 29) assert.equal(d.leave, false);
    else assert.equal(d.leave, true);
  }
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

// --- накопитель пика уровня (между опросами цикла раз в 10 s) ---

import { pushSample, readPeak } from "../src/silence.js";

const empty = () => ({ peak: null, seen: false });

test("pushSample: pustoy chital i pik — pik novogo", () => {
  assert.deepEqual(pushSample(empty(), -60), { peak: -60, seen: true });
});

test("pushSample: kopyot MAKSIMUM, a ne posledniy", () => {
  let st = pushSample(empty(), -70);
  st = pushSample(st, -30);
  st = pushSample(st, -55);
  assert.deepEqual(st, { peak: -30, seen: true });
});

test("pushSample: null/undefined/NaN ni sbrasyvayet ni popolyayet okno", () => {
  const st = pushSample({ peak: -30, seen: true }, null);
  assert.deepEqual(st, { peak: -30, seen: true });
  assert.deepEqual(pushSample({ peak: -30, seen: true }, undefined),
    { peak: -30, seen: true });
  assert.deepEqual(pushSample({ peak: -30, seen: true }, NaN),
    { peak: -30, seen: true });
});

test("readPeak: puskoe okno — null (nie путать с tişinoy)", () => {
  const { levelDb, next } = readPeak(empty());
  assert.equal(levelDb, null);
  assert.deepEqual(next, empty());
});

test("readPeak: vazvraschaet pik i ochishchayet okno", () => {
  const { levelDb, next } = readPeak({ peak: -33.5, seen: true });
  assert.equal(levelDb, -33.5);
  assert.deepEqual(next, empty());
});

test("pik reki perezhivyayet tiku bez izmereniy (net analyzera/ctx)", () => {
  let st = pushSample(empty(), -40);
  st = pushSample(st, null);
  const { levelDb, next } = readPeak(st);
  assert.equal(levelDb, -40);
  assert.equal(isSoundLevel(levelDb, -50), true);
  assert.deepEqual(next, empty());
});

test("regressiya job 9: fon mezhu frazami na tikhah + odin tik s rech'yu — pik vyše poroga", () => {
  let st = empty();
  // 10 s tikov: 99 % — comfort-noise mezhu frazami, edinye tiku s rech'yu
  for (let i = 0; i < 99; i++) st = pushSample(st, -65);
  st = pushSample(st, -35); // 1 % tik popal v frazu
  const { levelDb } = readPeak(st);
  assert.equal(isSoundLevel(levelDb, -50), true);
});

// --- shouldAskConfirmEnd: bezopasnyy voprosh operatoru posle endAt (job 14/15) ---

test("confirmEnd: do planovovo okonchaniya — nie sprašivaem", () => {
  assert.equal(shouldAskConfirmEnd({
    nowMs: 100_000, endAtMs: 200_000, nullStreak: 100, blindMin: 9,
    confirmAskedTs: null,
  }), false);
});

test("confirmEnd: yesli endAt nie zadano — nie sprašivaem (grace-rezhim)", () => {
  assert.equal(shouldAskConfirmEnd({
    nowMs: 100_000, endAtMs: null, nullStreak: 100, blindMin: 9,
    confirmAskedTs: null,
  }), false);
});

test("confirmEnd: detektor nie slepoy — nie sprašivaem", () => {
  assert.equal(shouldAskConfirmEnd({
    nowMs: 300_000, endAtMs: 200_000, nullStreak: 8, blindMin: 9,
    confirmAskedTs: null,
  }), false);
});

test("confirmEnd: posle endAt i slepoy detektor — sprašyvaem", () => {
  assert.equal(shouldAskConfirmEnd({
    nowMs: 300_000, endAtMs: 200_000, nullStreak: 9, blindMin: 9,
    confirmAskedTs: null,
  }), true);
});

test("confirmEnd: uzhe sproshyvali — nie powtoryayem", () => {
  assert.equal(shouldAskConfirmEnd({
    nowMs: 300_000, endAtMs: 200_000, nullStreak: 50, blindMin: 9,
    confirmAskedTs: 250_000,
  }), false);
});
