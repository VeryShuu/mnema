// Testy čistych pomoshchnikov okonnogo rekorda (seg_key.js).
// Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { segKey, shortMixWarningS, ebmlHeaderOffsets, EBML_MAGIC, filterUsableSegs, mixWorseThanInput, pickBestDecoded } from "../src/seg_key.js";

const U = "https://corp.vip/w/my/join";

test("odinakovye url/name/okno bez smeny dokumenta — odin klyuch", () => {
  assert.equal(segKey(U, "zoom", "doc-1", 1), segKey(U, "zoom", "doc-1", 1));
});

test("smena docId (reload freyma) — drugoy klyuch, novyy segment-fayl", () => {
  assert.notEqual(segKey(U, "zoom", "doc-1", 1), segKey(U, "zoom", "doc-2", 1));
});

test("raznyye okna — raznyye klyuchi", () => {
  assert.notEqual(segKey(U, "zoom", "doc-1", 1), segKey(U, "zoom", "doc-1", 2));
});

test("otsutstvie docId — stabil'nyy nodoc, ne dayet kolliiziy s real'nym id", () => {
  assert.equal(segKey(U, "zoom", undefined, 1), segKey(U, "zoom", null, 1));
  assert.notEqual(segKey(U, "zoom", undefined, 1), segKey(U, "zoom", "doc-1", 1));
});

test("mikš normal'noy dliny — bez preduprezhdeniya", () => {
  assert.equal(shortMixWarningS(2 * 3600, 2 * 3600 + 5), null);
});

test("mikš korotche wall-clock boleye slack — warning s deficitom", () => {
  const w = shortMixWarningS(936, 2 * 3600 + 8 * 60);
  assert.ok(w !== null && w > 6000);
});

test("v predelakh slack (60 s) — bez warning", () => {
  assert.equal(shortMixWarningS(3600, 3600 + 30), null);
});

test("korotkaya vstrecha (<2 min) — ne checkim", () => {
  assert.equal(shortMixWarningS(10, 60), null);
});

test("durationS null (ffprobe ne otvetil) — bez warning", () => {
  assert.equal(shortMixWarningS(null, 7200), null);
});

test("wall-clock NaN / null — bez warning", () => {
  assert.equal(shortMixWarningS(600, NaN), null);
  assert.equal(shortMixWarningS(600, null), null);
});

test("odin potok — odin offset v nachale", () => {
  const buf = Buffer.concat([EBML_MAGIC, Buffer.alloc(100, 7)]);
  assert.deepEqual(ebmlHeaderOffsets(buf), [0]);
});

test("dva skleennykh potoka — dva offseta (pattern job 16)", () => {
  const buf = Buffer.concat([EBML_MAGIC, Buffer.alloc(50), EBML_MAGIC, Buffer.alloc(50)]);
  assert.deepEqual(ebmlHeaderOffsets(buf), [0, 54]);
});

test("pustoy bufer — pusty spisok", () => {
  assert.deepEqual(ebmlHeaderOffsets(Buffer.alloc(0)), []);
});

test("net magic — pusty spisok", () => {
  assert.deepEqual(ebmlHeaderOffsets(Buffer.from("xxxxxxxxxxxx")), []);
});

// --- filterUsableSegs (regressiya job 17: stabil' 955 B ubil amix tselikom) ---

test("segment men'she 1 KiB otbrasyvaetsya", () => {
  const out = filterUsableSegs([{ p: "a", bytes: 955 }, { p: "b", bytes: 5000 }]);
  assert.deepEqual(out.map((s) => s.p), ["b"]);
});

test("segment rovno 1 KiB — ostayetsya (granitsa)", () => {
  const out = filterUsableSegs([{ p: "a", bytes: 1024 }]);
  assert.equal(out.length, 1);
});

test("null/undefined v spiske — ne padayet, otbrasyvaetsya", () => {
  assert.deepEqual(filterUsableSegs([null, undefined, { p: "a", bytes: 2048 }]).map((s) => s.p), ["a"]);
  assert.deepEqual(filterUsableSegs(null), []);
  assert.deepEqual(filterUsableSegs(undefined), []);
});

test("bytes=0 / otsutstvuyushchiy bytes — otbrasyvaetsya", () => {
  assert.deepEqual(filterUsableSegs([{ p: "a", bytes: 0 }, { p: "b" }]), []);
});

// --- mixWorseThanInput: mikš korotche luchshego vkhoda (regressiya job 16/17) ---

test("mikš 72 s protiv vkhoda 3980 s — khuzhe (porog 60 s prevyšen)", () => {
  assert.equal(mixWorseThanInput(72, 3980), true);
});

test("mikš v predelakh slack — ne khuzhe", () => {
  assert.equal(mixWorseThanInput(3950, 3980), false);
  assert.equal(mixWorseThanInput(3981, 3980), false);
});

test("mikš 0 (dekod upal) pri zhivom vkhode — khuzhe", () => {
  assert.equal(mixWorseThanInput(0, 100), true);
  assert.equal(mixWorseThanInput(null, 100), true);
  assert.equal(mixWorseThanInput(NaN, 100), true);
});

test("vkhod 0/NaN (nichego ne chitaetsya) — ne khuzhe, fallback bessmyslen", () => {
  assert.equal(mixWorseThanInput(0, 0), false);
  assert.equal(mixWorseThanInput(50, NaN), false);
});

// --- pickBestDecoded: vybor remonta po polnomu dekod ---

test("ffmpeg-fix chitaet'sya dalshe, chem syroye+mkvmerge — pobezhdayet fix", () => {
  const w = pickBestDecoded([
    { p: "raw", decodedS: 3980 },
    { p: "mkv", decodedS: 72, fixed: true },
    { p: "ffx", decodedS: 3980, fixed: true },
  ]);
  assert.equal(w.p, "ffx");
});

test("mkvmerge syedal payload (72 s) — berem syroye, a ne 「pochinennyy»", () => {
  const w = pickBestDecoded([
    { p: "raw", decodedS: 3980 },
    { p: "mkv", decodedS: 72, fixed: true },
  ]);
  assert.equal(w.p, "raw");
});

test("nesto chiotsya (vse decodedS=0) — null", () => {
  assert.equal(pickBestDecoded([{ p: "a", decodedS: 0 }, { p: "b", decodedS: 0, fixed: true }]), null);
  assert.equal(pickBestDecoded([]), null);
  assert.equal(pickBestDecoded(null), null);
});

test("pustoy mkvmerge (decodedS=0) otklonyaetsya, syroye beretsya", () => {
  const w = pickBestDecoded([{ p: "raw", decodedS: 100 }, { p: "mkv", decodedS: 0, fixed: true }]);
  assert.equal(w.p, "raw");
});

test("fixed poluchaet bonus slack: syroye 100 vs fixed 99 — beretsya fixed", () => {
  const w = pickBestDecoded([{ p: "raw", decodedS: 100 }, { p: "ffx", decodedS: 99, fixed: true }]);
  assert.equal(w.p, "ffx");
});

test("fixed proigryayet s deficitom boleye slack — beretsya syroye", () => {
  const w = pickBestDecoded([{ p: "raw", decodedS: 200 }, { p: "ffx", decodedS: 139.9, fixed: true }]);
  assert.equal(w.p, "raw");
});
