// Testy flaga anonsa (config.js). Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { boolFlag, isAnnounceEnabled } from "../src/config.js";

test("pustoy env — anons vklyuchen po umolchaniyu", () => {
  assert.equal(isAnnounceEnabled({}), true);
  assert.equal(isAnnounceEnabled({ ANNOUNCE_ENABLED: "" }), true);
});

test("ANNOUNCE_ENABLED=0/false/off/no — vykluchen", () => {
  for (const v of ["0", "false", "off", "no", "OFF", "False", " 0 "])
    assert.equal(isAnnounceEnabled({ ANNOUNCE_ENABLED: v }), false, v);
});

test("ANNOUNCE_ENABLED=1/true/on/yes — vklyuchen", () => {
  for (const v of ["1", "true", "on", "yes", "TRUE"])
    assert.equal(isAnnounceEnabled({ ANNOUNCE_ENABLED: v }), true, v);
});

test("neraspoznnannoe znachenie — default", () => {
  assert.equal(isAnnounceEnabled({ ANNOUNCE_ENABLED: "mozhet-byt" }), true);
  assert.equal(boolFlag("mozhet-byt", false), false);
});

test("boolFlag bez znacheniya dayot default", () => {
  assert.equal(boolFlag(undefined, true), true);
  assert.equal(boolFlag(undefined, false), false);
  assert.equal(boolFlag(null, false), false);
});
