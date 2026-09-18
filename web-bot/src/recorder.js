// Zapis' vstrechi Zoom cherez vebl-klient (FT-1..10, FT-41).
// Tehnika po razdelu 3 TZ: pat RTCPeerConnection do navigacii, mikshirovanie
// udalennyh audiodorozhek v AudioContext, MediaRecorder(opus/webm), vykachivanie
// chankov kazhdye 5 s, proverka sostoyaniy stranitsy.
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { buildJoinUrl } from "./config.js";

// Health-check Zoom-сессии (FT-24): otkryt' app.zoom.us persistent-profilem i
// proverdity, ne trebuetsya li login (sessiya iva ~2 nedeli). Vozvr. true ok.
export async function healthCheckSession(cfg) {
  let ctx;
  try {
    ctx = await chromium.launchPersistentContext(cfg.zoomProfileDir, {
      headless: true, args: ["--use-fake-device-for-media-stream"],
    });
    const page = await ctx.newPage();
    await page.goto("https://app.zoom.us/wc/home", {
      waitUntil: "domcontentloaded", timeout: 40000,
    });
    const v = await classifyPage(page);
    const ok = v !== "login_required" && v !== "bot_wall";
    if (!ok) console.log("[health] zoom sessiya ne zhivaya:", v);
    return ok;
  } catch (e) {
    console.error("[health] oshibka provery:", e.message);
    return false;
  } finally {
    if (ctx) { try { await ctx.close(); } catch { /* */ } }
  }
}

const EVENT = { // FT-9
  NAVIGATING: "navigating", PREJOIN: "prejoin", WAITING_ROOM: "waiting_room",
  JOINED: "joined", ENDED: "ended", BOT_WALL: "bot_wall", LOGIN_REQUIRED: "login_required",
  TRACK_ADDED: "track_added", TRACK_REMOVED: "track_removed",
  CHUNKS_SAVED: "chunks_saved", SILENCE: "silence", RELOAD: "reload", ERROR: "error",
};

export async function launchAndRecord(jobId, cfg, holder, onEvent) {
  const jobDir = path.join(cfg.dataDir, "jobs", String(jobId));
  fs.mkdirSync(jobDir, { recursive: true });
  const webmPath = path.join(jobDir, "raw.webm");
  const wavPath = path.join(jobDir, "meeting.wav");
  const mp3Path = path.join(jobDir, "meeting.mp3");

  const ctx = await chromium.launchPersistentContext(cfg.zoomProfileDir, {
    headless: !cfg.headful,
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
      "--autoplay-policy=no-user-gesture-required",
    ],
    permissions: ["microphone"],
    viewport: { width: 1280, height: 800 },
  });
  const page = await ctx.newPage();

  const state = {
    stopRequested: false, reloads: 0, joinedAt: null, lastChunksTs: Date.now(),
    chunksSaved: 0,
  };
  holder.requestStop = () => { state.stopRequested = true; };

  await page.addInitScript(INIT_SCRIPT);

  // --- vykachivanie chankov kazhdye N s (FT-1): bazu ne teryaem pri padenii
  let recording = false;
  let lastOffset = 0;
  const file = fs.openSync(webmPath, "a");
  const flushTimer = setInterval(async () => {
    if (!recording) return;
    try {
      const b64 = await page.evaluate(([off]) =>
        (window.__mnemaDrain ? window.__mnemaDrain(off) : null), [lastOffset]);
      if (b64 && b64.data && b64.data.length) {
        fs.writeSync(file, Buffer.from(b64.data, "base64"));
        state.chunksSaved = b64.total;
        lastOffset = b64.total;
        state.lastChunksTs = Date.now();
        if (b64.total % 15 === 0)
          onEvent(EVENT.CHUNKS_SAVED, `total=${b64.total}`);
      }
      const st = await page.evaluate(() => window.__mnemaStatus ? window.__mnemaStatus() : null);
      if (st) {
        if (st.tracks !== undefined) onEvent(EVENT.TRACK_ADDED, `live_tracks=${st.tracks}`);
      }
    } catch (e) {
      onEvent(EVENT.ERROR, `flush: ${e.message}`);
    }
  }, cfg.chunkFlushMs);

  try {
    const attemptRes = await joinMeeting(page, cfg, state, onEvent, jobId);
    if (!attemptRes.ok) {
      return { ok: false, fail_reason: attemptRes.reason, error: attemptRes.error };
    }
    state.joinedAt = Date.now();
    recording = true;

    // Audio-obyavlenie (FT-10) srazu posle vhoda
    await playAnnouncement(page, cfg, onEvent);

    // uzhol konca vstrechi / tihi / hard-limit (FT-4)
    const end = await waitForEnd(page, cfg, state, onEvent);

    // ostannie chanki (do zaversheniya тайméra)
    try {
      const b64 = await page.evaluate(([off]) =>
        window.__mnemaDrain ? window.__mnemaDrain(off) : null, [lastOffset]);
      if (b64 && b64.data?.length) {
        fs.writeSync(file, Buffer.from(b64.data, "base64"));
        lastOffset = b64.total;
      }
    } catch { /* stranitsa uzhe mohla utochn */ }

    if (end.reason === "reload_exhausted") {
      return { ok: false, fail_reason: "page_crash",
               error: "stranitsa utochen posle vseh reloadov", webm: webmPath };
    }
    return finalize(jobId, webmPath, wavPath, mp3Path, state, end);
  } finally {
    clearInterval(flushTimer);
    try { fs.closeSync(file); } catch { /* already */ }
    await cleanupContext(ctx, jobId, cfg, onEvent);
  }
}

// ------------------------------------------------------------------ zahod
async function joinMeeting(page, cfg, state, onEvent, jobId) {
  const deadlineMs = Date.now() + (cfg.joinDeadlineMin * 60 - cfg.prejoinSeconds) * 1000;
  let joinUrl = process.env.MNEMA_JOIN_URL; // dlya testov: lokalnaya fixtura
  if (!joinUrl) {
    const meta = await fetchJobMeta(jobId);
    if (!meta) return { ok: false, reason: "timeout", error: "net meta job" };
    joinUrl = buildJoinUrl(cfg, meta.meeting_id, meta.pwd);
  }
  onEvent(EVENT.NAVIGATING, "to join url");

  while (Date.now() < deadlineMs && !state.stopRequested) {
    try {
      await page.goto(joinUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
    } catch (e) {
      if (!state.stopRequested) {
        onEvent(EVENT.ERROR, `goto: ${e.message}`);
        await page.waitForTimeout(5000);
        continue;
      }
      return { ok: false, reason: "timeout", error: String(e.message) };
    }

    const verdict = await classifyPage(page);
    if (verdict === "bot_wall") {
      onEvent(EVENT.BOT_WALL, "Automated bots aren't allowed");
      return { ok: false, reason: "zoom_requires_rtms",
               error: "anty-bot-stena (RTMS-only) na storone hosta" };
    }
    if (verdict === "login_required") {
      onEvent(EVENT.LOGIN_REQUIRED, "redirect Sign in");
      return { ok: false, reason: "auth_session_missing",
               error: "zoom-sessiya v profile nie zhivaya" };
    }
    if (verdict === "waiting_room") {
      onEvent(EVENT.WAITING_ROOM, "waiting for host");
      // host vypuskaet Mnema vuchnuyu; zhdём do deadline
      if (await waitForLeaveButton(page, deadlineMs)) {
        onEvent(EVENT.JOINED, "iz waiting room");
        return { ok: true };
      }
      if (classifyPage(page) === "waiting_room") {
        onEvent(EVENT.WAITING_ROOM, "stuck");
        return { ok: false, reason: "waiting_room_stuck",
                 error: "host ne vypustil iz waiting room" };
      }
      continue;
    }
    if (verdict === "meeting_ended") {
      return { ok: false, reason: "not_started", error: "vstrecha uze zasvershena" };
    }
    if (verdict === "prejoin") {
      onEvent(EVENT.PREJOIN, "imya + Join");
      await fillNameAndJoin(page, cfg.botName);
      if (await waitForLeaveButton(page, deadlineMs)) {
        onEvent(EVENT.JOINED, "s pred-join");
        return { ok: true };
      }
      const v2 = await classifyPage(page);
      if (v2 === "bot_wall") {
        return { ok: false, reason: "zoom_requires_rtms", error: "stena posle Join" };
      }
      continue;
    }
    if (verdict === "joined") {
      onEvent(EVENT.JOINED, "uže v zone");
      return { ok: true };
    }
    // hosts_ne_started/registry:
    onEvent(EVENT.PREJOIN, "zhdём starta vstrechi");
    await page.waitForTimeout(15000);
  }
  return { ok: false, reason: state.stopRequested ? "cancelled" : "not_started",
           error: state.stopRequested ? "ostanovleno vruchnuyu" : "deadline vhoda istёk" };
}

async function fetchJobMeta(jobId) {
  const API = process.env.API_URL || "http://api:8000";
  try {
    const r = await fetch(`${API}/api/jobs/${jobId}/join-info`);
    if (!r.ok) return null;
    return await r.json(); // {meeting_id, pwd|null}
  } catch {
    return null;
  }
}

// Klassifikator sostoyaniya stranitsy po neskolkim priznakam (FT-4/12 'hristkost' DOM).
export async function classifyPage(page) {
  try {
    const info = await page.evaluate(() => {
      const txt = document.body ? document.body.innerText : "";
      const has = (s) => txt.toLowerCase().includes(s.toLowerCase());
      const leaveBtn =
        document.querySelector("[aria-label='Leave meeting']") ||
        document.querySelector("[data-variant='danger'] :text('Leave')") ||
        [...document.querySelectorAll("button")].some((b) => /leave/i.test(b.innerText));
      const joinBtn = [...document.querySelectorAll("button")].some(
        (b) => /^(Join|Join from your browser)$/i.test(b.innerText.trim()));
      return {
        leaveBtn, joinBtn,
        botWall: has("Automated bots aren't allowed") || has("must use a Zoom app") || has("Zoom Workplace app"),
        signRequired: has("Sign in to continue to Zoom"),
        waitingRoom: has("Please wait for the host to start this meeting") ||
                     has("waiting room") || !!document.querySelector("#waitingSessionTitle"),
        ended: has("The meeting has ended") || has("This meeting has ended"),
      };
    });
    if (info.signRequired) return "login_required";
    if (info.botWall) return "bot_wall";
    if (info.ended) return "meeting_ended";
    if (info.leaveBtn) return "joined";
    if (info.waitingRoom) return "waiting_room";
    if (info.joinBtn) return "prejoin";
    return "pending";
  } catch (e) {
    return "pending";
  }
}

async function fillNameAndJoin(page, botName) {
  try {
    const nameInput = page.locator("input[type='text']").first();
    if (await nameInput.isVisible({ timeout: 5000 }).catch(() => false)) {
      await nameInput.fill(botName);
    }
    const joinBtn = page.getByRole("button", { name: /^(Join|Join from your browser)$/i }).first();
    if (await joinBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
      await joinBtn.click();
    }
  } catch (e) {
    /* prozhzhaem: mogut byt' drugie selektory — classify razberiotsya */
  }
}

async function waitForLeaveButton(page, deadlineMs) {
  while (Date.now() < deadlineMs) {
    const v = await classifyPage(page);
    if (v === "joined") return true;
    if (v === "bot_wall" || v === "login_required" || v === "meeting_ended") return false;
    await page.waitForTimeout(3000);
  }
  return false;
}

// -------------------------------------------------------------- konets zapiši
async function waitForEnd(page, cfg, state, onEvent) {
  const hardMs = cfg.maxDurationMin * 60 * 1000;
  while (!state.stopRequested) {
    await page.waitForTimeout(10000);
    // prinyuditelny limit
    if (Date.now() - state.joinedAt > hardMs) {
      onEvent(EVENT.ENDED, "max duration");
      return { reason: "max_duration" };
    }
    if (state.stopRequested) return { reason: "stopped" };
    const v = await classifyPage(page);
    if (v === "meeting_ended") {
      onEvent(EVENT.ENDED, "page: meeting ended");
      return { reason: "meeting_ended" };
    }
    if (v === "pending") {
      // stranitsa mogla upasť: proverka zhivee li recorder
      const alive = await page.evaluate(() => !!window.__mnemaDrain).catch(() => false);
      if (!alive) {
        state.reloads++;
        if (state.reloads > cfg.pageReloadRetries) {
          return { reason: "reload_exhausted" };
        }
        onEvent(EVENT.RELOAD, `reload #${state.reloads}`);
        await page.reload({ waitUntil: "domcontentloaded" }).catch(() => {});
        continue;
      }
    } else {
      state.reloads = 0; // strannitsa zhivaya
    }
    // tišina: esli chanki ne idut dol'še SILENCE_END_TIMEOUT — proverit str.
    const silenceMin = (Date.now() - state.lastChunksTs) / 60000;
    if (silenceMin > cfg.silenceEndTimeoutMin) {
      onEvent(EVENT.SILENCE, `bez chankov ${silenceMin.toFixed(1)} min`);
      const v2 = await classifyPage(page);
      if (v2 !== "joined") {
        onEvent(EVENT.ENDED, "tišina + nie v zone");
        return { reason: "silence_then_not_joined" };
      }
      state.lastChunksTs = Date.now(); // esli v zone — prodolzhaem zhidat'
    }
  }
  return { reason: "stopped" };
}

// ------------------------------------------------------------- finalize FT-6/41
function finalize(jobId, webmPath, wavPath, mp3Path, state, end) {
  let durationS = null;
  // pocinka povrezhdёnogo webm (FT-6), yesli nuzno
  if (!probeDuration(webmPath)) {
    try {
      const fixed = webmPath + ".fixed.webm";
      execFileSync("mkvmerge", ["-o", fixed, webmPath], { stdio: "pipe" });
      if (fs.existsSync(fixed) && fs.statSync(fixed).size > 0) {
        fs.renameSync(fixed, webmPath);
      }
    } catch (e) {
      /* mkvmerge nie vsetda spasayet; prodiolzhajem so syryom */
    }
  }
  try {
    durationS = probeDuration(webmPath);
  } catch { durationS = null; }
  // wav i mp3 (mp3 srazu — FT-41a)
  let wavOk = false, mp3Ok = false;
  try {
    execFileSync("ffmpeg", ["-y", "-err_detect", "ignore_err", "-i", webmPath,
      "-ar", "16000", "-ac", "1", wavPath], { stdio: "pipe" });
    wavOk = fs.existsSync(wavPath);
  } catch (e) { /* worker povtorit */ }
  try {
    execFileSync("ffmpeg", ["-y", "-err_detect", "ignore_err", "-i", webmPath,
      "-ar", "44100", "-ac", "1", "-b:a", "64k", mp3Path], { stdio: "pipe" });
    mp3Ok = fs.existsSync(mp3Path);
  } catch (e) { /* mp3 encoder niedostupen — razresheno vydat webm */ }
  const sizes = {};
  for (const [k, p] of [["webm", webmPath], ["wav", wavPath], ["mp3", mp3Path]])
    if (fs.existsSync(p)) sizes[k] = fs.statSync(p).size;
  return {
    ok: true, webm: webmPath, wav: wavOk ? wavPath : null,
    mp3: mp3Ok ? mp3Path : null, duration_s: durationS, sizes,
    end_reason: end.reason,
  };
}

function probeDuration(p) {
  try {
    const out = execFileSync("ffprobe", ["-v", "quiet", "-print_format", "json",
      "-show_format", p], { encoding: "utf8" });
    return parseFloat(JSON.parse(out).format.duration) || 0;
  } catch { return 0; }
}

async function cleanupContext(ctx, jobId, cfg, onEvent) {
  try { await ctx.close(); } catch (e) { onEvent(EVENT.ERROR, `ctx close: ${e.message}`); }
}

// ------------------------------------------------------------- obyavlenie FT-10
async function playAnnouncement(page, cfg, onEvent) {
  const wavPath = path.resolve(cfg.announceWav);
  if (!fs.existsSync(wavPath)) {
    onEvent(EVENT.ERROR, "announce.wav nie nayden — propusk");
    return;
  }
  try {
    const buf = fs.readFileSync(wavPath);
    await page.evaluate(async (b64) => {
      if (!window.__mnemaPlayAnnouncement) return;
      await window.__mnemaPlayAnnouncement(b64);
    }, buf.toString("base64"));
    onEvent("announce", "obyavlenie vosproizvedeno");
  } catch (e) {
    onEvent(EVENT.ERROR, `announce: ${e.message}`);
  }
}

// ============================================================ INIT SCRIPT
// Vpryskivaetsya do navigacii (addInitScript). Pat RTCPeerConnection:
// kazhdy udalenny audiotrek -> AudioContext -> miksh -> MediaRecorder.
// window.__mnemaDrain(offset) -> {data: base64 chankov s offset, total}.
const INIT_SCRIPT = `
(function () {
  window.__mnemaChunks = [];
  window.__mnemaTotal = 0;
  window.__mnemaTracks = 0;
  const OrigPC = window.RTCPeerConnection;
  let ctx = null, dest = null, rec = null;

  function ensurePipeline() {
    if (ctx) return;
    ctx = new AudioContext();
    dest = ctx.createMediaStreamDestination();
    rec = new MediaRecorder(dest.stream, { mimeType: 'audio/webm' });
    rec.ondataavailable = (ev) => {
      if (ev.data && ev.data.size > 0) {
        ev.data.arrayBuffer().then((ab) => {
          const u8 = new Uint8Array(ab);
          let bin = '';
          const slice = 0x8000;
          for (let i = 0; i < u8.length; i += slice)
            bin += String.fromCharCode.apply(null, u8.subarray(i, i + slice));
          window.__mnemaChunks.push({ b64: btoa(bin), len: u8.byteLength });
          window.__mnemaTotal += u8.byteLength;
        });
      }
    };
    rec.start(4000);
  }

  function attachTrack(track) {
    if (track.kind !== 'audio') return;
    ensurePipeline();
    try {
      const src = ctx.createMediaStreamSource(new MediaStream([track]));
      src.connect(dest);
      window.__mnemaTracks++;
    } catch (e) { /* uze podat' ili nedastupno */ }
  }

  window.RTCPeerConnection = function (...args) {
    const pc = new OrigPC(...args);
    const origAddTrack = pc.addTrack.bind(pc);
    pc.addTrack = function (track, ...rest) { return origAddTrack(track, ...rest); };
    pc.addEventListener('track', (ev) => {
      if (ev.track && ev.transceiver && ev.transceiver.direction === 'recvonly') {
        attachTrack(ev.track);
      } else if (ev.track) attachTrack(ev.track);
    });
    const origOnTrack = Object.getOwnPropertyDescriptor(OrigPC.prototype, 'ontrack');
    Object.defineProperty(pc, 'ontrack', {
      set(fn) { pc.addEventListener('track', fn); },
      get() { return null; },
    });
    return pc;
  };
  window.RTCPeerConnection.prototype = OrigPC.prototype;

  // Vyachka chankov s nachala offset; keshiraem posledniy vraz (idempotentnost')
  window.__mnemaDrain = function (offsetBytes) {
    const out = [];
    let pos = 0;
    for (const c of window.__mnemaChunks) {
      if (pos + c.len <= offsetBytes) { pos += c.len; continue; }
      if (pos >= offsetBytes) out.push(c.b64);
      pos += c.len;
    }
    window.__mnemaChunks = []; // vraz sbroshen; sled. vyachka s total
    return { data: out.join(''), total: window.__mnemaTotal };
  };
  window.__mnemaStatus = function () {
    return { tracks: window.__mnemaTracks, bytes: window.__mnemaTotal,
             recording: rec && rec.state === 'recording' };
  };

  // FT-10: proigrat wav v isodshiy audiotrek (miksh v MediaStreamDestination ne
  // podhodit — idёт v udalennoyu storonu; ispolzuem isodshiy send-track yesli on yest).
  window.__mnemaPlayAnnouncement = async function (b64) {
    ensurePipeline();
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const audioBuf = await ctx.decodeAudioData(bytes.buffer);
    const srcNode = ctx.createBufferSource();
    srcNode.buffer = audioBuf;
    // v udaliyu storonu: my mychim, no obyavlenie dolzhno slishat' uchastniki.
    // Vexa-style: vslivaem v khod, chto idyot kak 'mikrofon' (fake device).
    // Dostavka: sozdayem otdelny MediaStream-track i probuem zamenit send-track.
    const annDest = ctx.createMediaStreamDestination();
    srcNode.connect(annDest);
    srcNode.start();
    // best-effort: podyaem na yavnuyu globalnuyu funktsiyu dlya vneshney zameny
    window.__mnemaAnnouncementStream = annDest.stream;
    return true;
  };
})();
`;
