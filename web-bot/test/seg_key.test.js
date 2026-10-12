// Testy čistych pomoshchnikov okonnogo rekorda (seg_key.js).
// Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { segKey, shortMixWarningS, ebmlHeaderOffsets, EBML_MAGIC } from "../src/seg_key.js";

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
