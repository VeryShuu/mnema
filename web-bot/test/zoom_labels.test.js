// Testy predikatov metok knopok Zoom (zoom_labels.js). Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  isMicCandidate, micMutedFromLabel,
  isLeaveMeetingLabel, isEndMeetingLabel, isJoinedControlLabel,
} from "../src/zoom_labels.js";

// --- mikrofon ---------------------------------------------------------------
test("mikrofona-kandidat: angliyskiye i russkiye metki", () => {
  assert.equal(isMicCandidate("Mute my microphone"), true);
  assert.equal(isMicCandidate("Unmute"), true);
  assert.equal(isMicCandidate("Выключить микрофон"), true);
  assert.equal(isMicCandidate("Stop audio"), true);
});

test("mikrofona-kandidat: chuzhie knopki ne podhodiat", () => {
  assert.equal(isMicCandidate("Participants"), false);
  assert.equal(isMicCandidate("End meeting"), false);
  assert.equal(isMicCandidate("Share screen"), false);
});

test("aktivnyy mikrofon (klik vyklyuchit) — muted false", () => {
  assert.equal(micMutedFromLabel("Mute my microphone"), false);
  assert.equal(micMutedFromLabel("MUTE"), false);
  assert.equal(micMutedFromLabel("Stop audio"), false);
  assert.equal(micMutedFromLabel("Выключить микрофон"), false);
});

test("vyklyuchennyy mikrofon — muted true", () => {
  assert.equal(micMutedFromLabel("Unmute"), true);
  assert.equal(micMutedFromLabel("Unmute microphone"), true);
  assert.equal(micMutedFromLabel("Start audio"), true);
  assert.equal(micMutedFromLabel("Включить микрофон"), true);
});

test("nieopredelyonnaya nadpis' — null (nie klikat' vslepuyu)", () => {
  assert.equal(micMutedFromLabel("Audio"), null);
  assert.equal(micMutedFromLabel(""), null);
  assert.equal(micMutedFromLabel(null), null);
});

// --- Leave / End -------------------------------------------------------------
test("Leave-varianty v obychnoy vstreche", () => {
  assert.equal(isLeaveMeetingLabel("Leave meeting"), true);
  assert.equal(isLeaveMeetingLabel("Leave"), true);
  assert.equal(isLeaveMeetingLabel("Выйти из встречи"), true);
  assert.equal(isLeaveMeetingLabel("Оставить встречу"), true);
});

test("End-varianty v personal'noy komnate (PMN, vstrecha 39)", () => {
  assert.equal(isEndMeetingLabel("End"), true);
  assert.equal(isEndMeetingLabel("end"), true);
  assert.equal(isEndMeetingLabel("End meeting"), true);
  assert.equal(isEndMeetingLabel("End meeting for all"), true);
  assert.equal(isEndMeetingLabel("End for all"), true);
  assert.equal(isEndMeetingLabel("Завершить встречу"), true);
});

test("End ne lovit lozhnyye sovmesteniya", () => {
  assert.equal(isEndMeetingLabel("Recommend"), false);
  assert.equal(isEndMeetingLabel("Send"), false);
  assert.equal(isEndMeetingLabel("Attendee list"), false);
  assert.equal(isEndMeetingLabel("Calendar"), false);
  assert.equal(isLeaveMeetingLabel("Leaving soon"), false);
});

test("priznak zona (joined): Leave ili End", () => {
  assert.equal(isJoinedControlLabel("Leave"), true);
  assert.equal(isJoinedControlLabel("End"), true);
  assert.equal(isJoinedControlLabel("Mute my microphone"), false);
  assert.equal(isJoinedControlLabel("Share screen"), false);
});
