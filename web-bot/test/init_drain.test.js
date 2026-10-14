// Test protokola drain/ack/gap v INIT_SCRIPT (recorder.js) BEZ playwright:
// vyryvaem strokу INIT_SCRIPT, vpulnyaem v vm-kontekste s zaglushkami
// MediaRecorder/AudioContext/RTCPeerConnection i proveryaem kluchevuju logiku
// zachishchayushchuyu audio ot poteri: ne-destruktivnyj drain, idempotentnyj
// ack, gap po retention, revive merzvogo payplayna.
import { test } from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(__dirname, "..", "src", "recorder.js"), "utf8");
const startTag = "const INIT_SCRIPT = `";
const s = src.indexOf(startTag);
const e = src.indexOf("`;", s);
const SCRIPT = src.slice(s + startTag.length, e);

function b64(str) { return Buffer.from(str, "utf8").toString("base64"); }
function unb64(d) { return Buffer.from(d, "base64").toString("utf8"); }

// Minimalnyy dom-browser: AudioContext s 'running', MediaRecorder zapisyvaet
// chanki vruchnuyu (emit). window = globalThere contexta (INIT_SCRIPT pishet
// i window.X, i gołyje crypto/Date/MediaRecorder).
function makeCtx() {
  class FakeMediaStream {
    constructor(t) { this._t = t || []; }
    getAudioTracks() { return this._t; }
    getVideoTracks() { return []; }
  }
  class FakeAudioContext {
    constructor() { this.state = "running"; }
    resume() { this.state = "running"; return Promise.resolve(); }
    createMediaStreamDestination() { return { stream: new FakeMediaStream([{}]) }; }
    createAnalyser() {
      return { fftSize: 2048, connect() {},
        getFloatTimeDomainData(a) { for (let i = 0; i < a.length; i++) a[i] = 0.1; } };
    }
  }
  FakeAudioContext.prototype.createMediaStreamSource = function () {
    return { connect() {} };
  };
  class FakeMediaRecorder {
    constructor(stream) {
      this.stream = stream; this.state = "inactive";
      FakeMediaRecorder.last = this;
    }
    start() { this.state = "recording"; }
    stop() { this.state = "inactive"; if (this.onstop) this.onstop(); }
    // vyzov simullyuiruyet ondataavailable s dannymi
    emit(sizeStr) {
      const u8 = new Uint8Array(Buffer.from(sizeStr, "utf8"));
      const ab = u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength);
      const data = { size: sizeStr.length, arrayBuffer: () => Promise.resolve(ab) };
      this.ondataavailable({ data });
    }
  }
  const sandbox = {
    navigator: { mediaDevices: {} },
    crypto: { randomUUID: () => "doc-1" },
    btoa: (b) => Buffer.from(b, "binary").toString("base64"),
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    MediaRecorder: FakeMediaRecorder,
    AudioContext: FakeAudioContext,
    RTCPeerConnection: class { addEventListener() {} addTrack() {} },
    HTMLMediaElement: class { static get srcObject() { return null; } },
    MediaStream: FakeMediaStream,
  };
  vm.createContext(sandbox);
  vm.runInContext("window = globalThis;", sandbox);
  vm.runInContext(SCRIPT, sandbox);
  return { win: sandbox, MediaRecorder: FakeMediaRecorder };
}

// Sozdast payplayn kak v reale: attach audio-dorozhki sozdaet AudioContext
// (patched createMediaStreamSource), zatem __mnemaArm otkryvaet okno 1 s rec.
function armPipeline(t) {
  const ac = new t.win.AudioContext();
  ac.createMediaStreamSource(new t.win.MediaStream([{ kind: "audio" }]));
  t.win.__mnemaArm();
  return t.MediaRecorder.last;
}

test("__mnemaDrain ne destructive: chanki zhivut do ack", async () => {
  const t = makeCtx();
  const rec = armPipeline(t);
  rec.emit("AAA");   // 3 bajt
  await new Promise((r) => setImmediate(r));
  const d1 = t.win.__mnemaDrain();
  assert.ok(d1.wins["1"], "okno 1 dolzhno soderzhat' entry");
  assert.equal(t.win.__mnemaStatus().win, 1);
  const d2 = t.win.__mnemaDrain();
  assert.deepEqual(d2.wins["1"], d1.wins["1"], "povtornyj drain bez ack dayet te zhe chanki");
});

test("__mnemaAck udelayet tol'ko podytverzhdjonnoye, ostal'noye zhdjot", async () => {
  const t = makeCtx();
  const rec = armPipeline(t);
  rec.emit("AA"); rec.emit("BB"); rec.emit("CC");
  await new Promise((r) => setImmediate(r));
  // 3 chanka po 2 bajt: off 0/2/4
  let d = t.win.__mnemaDrain();
  assert.equal(d.wins["1"].length, 3);
  // ack tol'ko pervomu chunku (upTo=2): ostajutsya vtoroy i tretiy
  t.win.__mnemaAck({ 1: 2 });
  d = t.win.__mnemaDrain();
  assert.equal(d.wins["1"].length, 2, "udalilsya tol'ko ack-nutyy chunk");
  assert.equal(d.wins["1"][0].off, 2, "schema nachalsya s neacknutogo");
  // ack za vse -> pustye wins
  t.win.__mnemaAck({ 1: 6 });
  d = t.win.__mnemaDrain();
  assert.equal(d.wins["1"], undefined, "vse ack-nuto — wins pust");
});

test("drain s ne-zavershennym chunkom ostanavlivaetsya (poryadok ne narushaetsya)", async () => {
  const t = makeCtx();
  const rec = armPipeline(t);
  rec.emit("AA");
  // vtoroy chunk s vechno-vyushchim arrayBuffer (b64 nie pridet)
  rec.ondataavailable({ data: { size: 2, arrayBuffer: () => new Promise(() => {}) } });
  await new Promise((r) => setImmediate(r));
  const d = t.win.__mnemaDrain();
  const entries = d.wins["1"] || [];
  assert.equal(entries.length, 1, "tol'ko zapolnennyy chunk popadaet v drain");
  assert.equal(unb64(entries[0].b64), "AA");
  assert.ok(!entries.some((x) => x.gap), "ne-zavershennyj chunk nie gap, a stoy");
});

test("__mnemaReviveIfStalled signalit kogda MediaRecorder umer", async () => {
  const t = makeCtx();
  const rec = armPipeline(t);
  assert.equal(t.win.__mnemaReviveIfStalled(), false, "zhivoy payplayn — false");
  rec.state = "inactive"; // simulyaciya smerti MediaRecorder
  assert.equal(t.win.__mnemaReviveIfStalled(), true, "umer — true");
});

test("__mnemaReviveIfStalled ozhivlyayet: novoye okno i novyy rec", async () => {
  const t = makeCtx();
  const rec = armPipeline(t);
  rec.state = "inactive";
  t.win.__mnemaReviveIfStalled();
  await new Promise((r) => setImmediate(r));
  const st = t.win.__mnemaStatus();
  assert.equal(st.win, 2, "revive otkryl okno 2");
  assert.equal(st.recording, true, "novyy MediaRecorder pishet");
});

test("__mnemaStatus dayet unackedBytes i ctxState dlya disk-gard-a", async () => {
  const t = makeCtx();
  const rec = armPipeline(t);
  rec.emit("AAAA");
  await new Promise((r) => setImmediate(r));
  const st = t.win.__mnemaStatus();
  assert.equal(st.recording, true);
  assert.equal(st.win, 1);
  assert.equal(st.ctxState, "running");
  assert.equal(st.unackedBytes, 4);
  t.win.__mnemaAck({ 1: 4 });
  assert.equal(t.win.__mnemaStatus().unackedBytes, 0);
});

test("retention: davnie ne-ack chanki vydralyvayutsya, obrazuya gap marker", async () => {
  const t = makeCtx();
  const rec = armPipeline(t);
  // 60 MB ne-ack chankov (sverh RETENTION_CAP=32MB, chunk ~1MB)
  const mb = "x".repeat(1024 * 1024);
  for (let i = 0; i < 60; i++) rec.emit(mb);
  await new Promise((r) => setImmediate(r));
  const st = t.win.__mnemaStatus();
  assert.ok(st.unackedBytes <= 32.7 * 1024 * 1024,
    `retained ${st.unackedBytes} ne dolzhno prevyshat' kap`);
  assert.ok(st.unackedBytes > 1024 * 1024, "cvezhie chanki sohraneny");
  const d = t.win.__mnemaDrain();
  // pervye entries — gap (vyrzukeno po retention), poslednie — b64
  assert.ok(d.wins["1"].some((x) => x.gap), "dolzhen byt' gap-marter vydrsknutyh");
  assert.ok(d.wins["1"].some((x) => x.b64), "cvezhie chanki s b64");
});

test("gap po retention: dannye na kontse okna sohraneny (59 MB s b64)", async () => {
  const t = makeCtx();
  const rec = armPipeline(t);
  const mb = "x".repeat(1024 * 1024);
  for (let i = 0; i < 60; i++) rec.emit(mb);
  await new Promise((r) => setImmediate(r));
  const d = t.win.__mnemaDrain();
  const entries = d.wins["1"];
  // posledniy entry = svyazhyy 1MB chunk v kontse okna (off = 59MB)
  const lastReal = [...entries].reverse().find((x) => x.b64);
  assert.ok(lastReal);
  assert.equal(lastReal.off, 59 * 1024 * 1024);
  assert.equal(lastReal.len, 1024 * 1024);
  // gap-pokrytie: summa gap >= vydralivannogo; summa gap+b64 dayet polnyy
  // pokrov okna (gap->nuli, b64->payload: idempotentno v writeEntry)
  let gap = 0;
  for (const e of entries) if (e.gap) gap += e.len;
  assert.ok(gap >= 20 * 1024 * 1024);
});
