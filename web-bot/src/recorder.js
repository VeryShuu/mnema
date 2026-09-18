// Zapis' vstrechi Zoom cherez vebl-klient (FT-1..10, FT-41).
// Tehnika po razdelu 3 TZ: pat RTCPeerConnection do navigacii, mikshirovanie
// udalennyh audiodorozhek v AudioContext, MediaRecorder(opus/webm), vykachivanie
// chankov kazhdye 5 s, proverka sostoyaniy stranitsy.
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { buildJoinUrl } from "./config.js";

function cleanSingletonLocks(profileDir) {
  // Ostatochnye blokirovki Chromium (upavshiy protsess / drugoy konteyner):
  // bez udaleniya profil' nie otkroetsya (exitCode=21).
  for (const f of ["SingletonLock", "SingletonCookie", "SingletonSocket"]) {
    try { fs.rmSync(path.join(profileDir, f), { force: true }); } catch { /* net — norm */ }
  }
}

// Health-check Zoom-сессии (FT-24): otkryt' app.zoom.us persistent-profilem i
// proverdity, ne trebuetsya li login (sessiya iva ~2 nedeli). Vozvr. true ok.
export async function healthCheckSession(cfg) {
  cleanSingletonLocks(cfg.zoomProfileDir);
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

export const EVENT = { // FT-9
  NAVIGATING: "navigating", PREJOIN: "prejoin", WAITING_ROOM: "waiting_room",
  JOINED: "joined", ENDED: "ended", BOT_WALL: "bot_wall", LOGIN_REQUIRED: "login_required",
  TRACK_ADDED: "track_added", TRACK_REMOVED: "track_removed",
  CHUNKS_SAVED: "chunks_saved", SILENCE: "silence", RELOAD: "reload", ERROR: "error",
};

export async function launchAndRecord(jobId, cfg, holder, onEvent) {
  const jobDir = path.join(cfg.dataDir, "jobs", String(jobId));
  fs.mkdirSync(jobDir, { recursive: true });
  cleanSingletonLocks(cfg.zoomProfileDir);
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
    lastNavTs: Date.now(), chunksSaved: 0,
  };
  holder.requestStop = () => { state.stopRequested = true; };

  await page.addInitScript(INIT_SCRIPT);

  // --- vykachivanie chankov kazhdye N s (FT-1): iz VSEH freymov (vstrecha
  //     Zoom zhivyot v dochernem iframe — v main frame audio net)
  let recording = false;
  let lastOffset = 0;
  const frameOffsets = new Map(); // frame -> kol-vo vykachannyh bait
  const file = fs.openSync(webmPath, "a");
  const flushTimer = setInterval(async () => {
    if (!recording) return;
    try {
      let gotAny = false;
      let tracks = 0;
      for (const fr of page.frames()) {
        const off = frameOffsets.get(fr) || 0;
        let b64 = null;
        try {
          b64 = await fr.evaluate((o) =>
            (window.__mnemaDrain ? window.__mnemaDrain(o) : null), [off]);
        } catch (e) { frameOffsets.set(fr, 0); continue; }
        if (!b64) continue;
        const st = await fr.evaluate(() => window.__mnemaStatus ? window.__mnemaStatus() : null).catch(() => null);
        if (st && st.tracks > tracks) tracks = st.tracks;
        if (b64.data && b64.data.length) {
          fs.writeSync(file, Buffer.from(b64.data, "base64"));
          frameOffsets.set(fr, b64.total);
          lastOffset = (lastOffset || 0) + b64.data.length;
          state.chunksSaved += b64.total - off;
          gotAny = true;
          state.lastChunksTs = Date.now();
        }
      }
      state.liveTracks = tracks;
      if (tracks > 0 && (flushTimer.counter = (flushTimer.counter || 0) + 1) % 6 === 0)
        onEvent(EVENT.TRACK_ADDED, `live_tracks=${tracks}`);
      if (gotAny && (flushTimer.saved = (flushTimer.saved || 0) + 1) % 12 === 0)
        onEvent(EVENT.CHUNKS_SAVED, `saved=${state.chunksSaved} bait`);
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

    // ostannie chanki iz vseh freymov
    for (const fr of page.frames()) {
      try {
        const off = frameOffsets.get(fr) || 0;
        const b64 = await fr.evaluate(([o]) =>
          window.__mnemaDrain ? window.__mnemaDrain(o) : null, [off]);
        if (b64 && b64.data?.length) {
          fs.writeSync(file, Buffer.from(b64.data, "base64"));
          frameOffsets.set(fr, b64.total);
        }
      } catch { /* stranitsa uzhe mohla utochn */ }
    }

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
// Oprasivanie klassifikatora poka chto-to raspoznanno (DOM za Gruzitsya dolgo).
async function pollClassify(page, timeoutMs, intervalMs = 2000) {
  const until = Date.now() + timeoutMs;
  let last = "pending";
  while (Date.now() < until) {
    last = await classifyPage(page);
    if (last !== "pending") return last;
    await page.waitForTimeout(intervalMs);
  }
  return last;
}

async function joinMeeting(page, cfg, state, onEvent, jobId) {
  const deadlineMs = Date.now() + (cfg.joinDeadlineMin * 60 - cfg.prejoinSeconds) * 1000;
  let joinUrl = process.env.MNEMA_JOIN_URL; // dlya testov: lokalnaya fixtura
  if (!joinUrl) {
    const meta = await fetchJobMeta(jobId);
    if (!meta) return { ok: false, reason: "timeout", error: "net meta job" };
    joinUrl = joinUrlFromMeta(cfg, meta);
  }
  onEvent(EVENT.NAVIGATING, "to join url");

  let navigated = false;
  while (Date.now() < deadlineMs && !state.stopRequested) {
    if (!navigated) {
      try {
        await page.goto(joinUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
        navigated = true;
      } catch (e) {
        onEvent(EVENT.ERROR, `goto: ${e.message}`);
        await page.waitForTimeout(5000);
        continue;
      }
    }

    const verdict = await pollClassify(page, 12000, 2000);
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
      return { ok: false, reason: "waiting_room_stuck",
               error: "host ne vypustil iz waiting room" };
    }
    if (verdict === "meeting_ended") {
      return { ok: false, reason: "not_started", error: "vstrecha uze zasvershena" };
    }
    if (verdict === "prejoin") {
      onEvent(EVENT.PREJOIN, "imya + Join");
      await fillNameAndJoin(page, cfg, onEvent);
      // reCAPTCHA: reshaet tol'ko chelovek (§12). Zhdём vruchnuyu prokhod
      // cherez noVNC (MAX CAPTCHA_WAIT_MIN), bez avtomatiki.
      if (await waitForLeaveOrCaptcha(page, deadlineMs, cfg, onEvent)) {
        onEvent(EVENT.JOINED, "s pred-join");
        return { ok: true };
      }
      continue; // eshche raz proitiremovat i popytats'
    }
    if (verdict === "joined") {
      onEvent(EVENT.JOINED, "uže v zone");
      return { ok: true };
    }
    // pending: zhdём; esli 1.5 min bez izmeneniy — odin reload dlya strahovki
    onEvent(EVENT.PREJOIN, "zhdём otobrazenia vstrechi");
    if (Date.now() - state.lastNavTs > 90000) {
      onEvent(EVENT.RELOAD, "stalyy pending - reload");
      navigated = false;
      state.lastNavTs = Date.now();
    }
  }
  return { ok: false, reason: state.stopRequested ? "cancelled" : "not_started",
           error: state.stopRequested ? "ostanovleno vruchnuyu" : "deadline vhoda istёk" };
}

function joinUrlFromMeta(cfg, meta) {
  // Pryamoy prioglashatel'nyy ssylki (PMN /my/..., /j/...) s parolem.
  if (meta.join_link) {
    let u = meta.join_link;
    if (meta.pwd && !/[?&]pwd=/.test(u)) {
      u += (u.includes("?") ? "&" : "?") + "pwd=" + encodeURIComponent(meta.pwd);
    }
    return u;
  }
  return buildJoinUrl(cfg, meta.meeting_id, meta.pwd);
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
// Vebl-klient Zoom: /my/... i /j/... otkryvayut zaglushku s iframe
// (meetings/iframe.html), gde lezhat knopki «Join from browser»/pre-join.
export async function classifyPage(page) {
  try {
    const frames = page.frames();
    let info = { leaveBtn: false, joinBrowser: false, join: false, micJoin: false,
                 botWall: false, signRequired: false, waitingRoom: false, ended: false,
                 captcha: false };
    for (const fr of frames) {
      const loc = await fr.evaluate(() => {
        const txt = document.body ? document.body.innerText : "";
        const has = (s) => txt.toLowerCase().includes(s.toLowerCase());
        const btns = [...document.querySelectorAll("button, a")].map(
          (b) => (b.innerText || b.getAttribute("aria-label") || "").trim().toLowerCase());
        return {
          leaveBtn: btns.some(t => /leave|выйти из встречи/.test(t)),
          joinBrowser: btns.some(t => /join from browser|войти из браузера/.test(t)),
          join: btns.some(t => t === "join" || t === "присоединиться"),
          micJoin: !!document.querySelector("#join_with_audio_link, #mic-button"),
          botWall: has("automated bots aren't allowed") || has("must use a zoom app") ||
                   has("join requires sign in") || has("rtms"),
          signRequired: has("sign in to continue to zoom"),
          waitingRoom: has("please wait for the host to start this meeting") ||
                       has("waiting for the host"),
          ended: has("the meeting has ended") || has("this meeting has ended"),
          captcha: /recaptcha/i.test(document.location.href)
                   || document.querySelector("iframe[src*='recaptcha'], .grecaptcha-badge") !== null
                   || has("protected by recaptcha"),
        };
      }).catch(() => null);
      if (!loc) continue;
      for (const k of Object.keys(info)) info[k] = info[k] || loc[k];
    }
    if (info.signRequired) return "login_required";
    if (info.botWall && !info.captcha) return "bot_wall";
    if (info.ended) return "meeting_ended";
    if (info.leaveBtn) return "joined";
    if (info.captcha) return "captcha";
    if (info.waitingRoom) return "waiting_room";
    if (info.joinBrowser || info.join || info.micJoin) return "prejoin";
    return "pending";
  } catch (e) {
    return "pending";
  }
}

// Knopku/pole iskhem vo VSEH freymah (vebl-klientZoom rizmesya po IFRAMEam).
async function clickAnyFrame(page, nameRe, waitMs = 3000) {
  const until = Date.now() + waitMs;
  while (Date.now() < until) {
    for (const fr of page.frames()) {
      const btn = fr.getByRole("button", { name: nameRe }).first();
      if (await btn.isVisible({ timeout: 500 }).catch(() => false)) {
        await btn.click({ timeout: 5000 }).catch(() => {});
        return true;
      }
      const link = fr.locator("a,div[role=button]").filter({ hasText: nameRe }).first();
      if (await link.isVisible({ timeout: 300 }).catch(() => false)) {
        await link.click({ timeout: 5000 }).catch(() => {});
        return true;
      }
    }
    await page.waitForTimeout(1000);
  }
  return false;
}

async function fillAnyFrame(page, botName, waitMs = 6000) {
  const until = Date.now() + waitMs;
  while (Date.now() < until) {
    for (const fr of page.frames()) {
      const inp = fr.locator("input[type='text']").first();
      if (await inp.isVisible({ timeout: 500 }).catch(() => false)) {
        await inp.fill(botName).catch(() => {});
        return true;
      }
    }
    await page.waitForTimeout(1000);
  }
  return false;
}

async function fillNameAndJoin(page, cfg, onEvent) {
  try {
    await clickAnyFrame(page, /accept cookies/i, 2500);
    // ekran «Join meeting» -> «Join from browser»
    if (await clickAnyFrame(page, /join from browser|войти из браузера/i, 4000)) {
      onEvent(EVENT.PREJOIN, "nata 'Join from browser'");
      await page.waitForTimeout(4000);
    }
    // pred-join: imya + Join
    await fillAnyFrame(page, cfg.botName, 8000);
    if (await clickAnyFrame(page, /^join$|присоединиться/i, 8000)) {
      onEvent(EVENT.PREJOIN, "nata 'Join'");
      await page.waitForTimeout(3000);
    }
    // vybor audiomarshruta (esli wyskakhyvaet okno)
    if (await clickAnyFrame(page, /join with (computer )?audio|войти с компьютер/i, 8000)) {
      onEvent(EVENT.PREJOIN, "nata 'Join with computer audio'");
    }
  } catch (e) {
    onEvent(EVENT.ERROR, `fillNameAndJoin: ${e.message}`);
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

// reCAPTCHA na knopke Join reshaet tol'ko chelovek (§12, kadptcha). Zhdёm
// vruchnuyu prokhoda cherez noVNC: signal + odnorazovyy alert, do CAPTCHA_WAIT_MIN.
let captchaAlerted = false;
async function waitForLeaveOrCaptcha(page, deadlineMs, cfg, onEvent) {
  const waitMs = (cfg.captchaWaitMin || 15) * 60 * 1000;
  const capUntil = Math.min(deadlineMs, Date.now() + waitMs);
  let sawCaptcha = false;
  while (Date.now() < capUntil) {
    const v = await classifyPage(page);
    if (v === "joined") return true;
    if (v === "captcha") {
      if (!sawCaptcha) {
        sawCaptcha = true;
        onEvent("captcha", "reCAPTCHA na Join — nuzhen vruchnuyu prokhod cherez noVNC :6080");
        if (!captchaAlerted) {
          captchaAlerted = true;
          alertOperator("reCAPTCHA pri vhode v vstrechu", "Otvorite noVNC :6080 (Connect) i proidite kapchu v okne Zoom. Avtomatizaciya kapchi zapreshchena (§12).");
        }
      }
    } else if (sawCaptcha && (v === "bot_wall" || v === "login_required" || v === "meeting_ended")) {
      return false;
    }
    await page.waitForTimeout(3000);
  }
  return false;
}

// Pryamoy SMTP signal cherez api (ne blokuyushchiy): proshu otdeliti v pish'mo
// cherez vnutrenniy endpoint, esli yest'; inache — console/notify ostanovitsya.
function alertOperator(subject, body) {
  const API = process.env.API_URL || "http://api:8000";
  fetch(`${API}/api/alert`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ subject, body, dedup_key: "captcha" }),
  }).catch((e) => console.error("alert fail:", e.message));
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
    if (track.kind !== 'audio' || track._mnemaHooked) return;
    track._mnemaHooked = true;
    ensurePipeline();
    try {
      const src = ctx.createMediaStreamSource(new MediaStream([track]));
      src.connect(dest);
      window.__mnemaTracks++;
    } catch (e) { /* uze podat' ili nedastupno */ }
  }

  function attachStream(stream) {
    try {
      for (const t of (stream && stream.getAudioTracks ? stream.getAudioTracks() : []))
        attachTrack(t);
    } catch (e) { /* */ }
  }

  // Zapis' audio idyot cherez WebAudio: lovimsya ksozdaniya istochnikov
  // MediaStream v Lyubom AudioContext (createMediaStreamSource), a takzhe
  // HTMLMediaElement.srcObject — eto lovit dorozhki daie bez sobytiy PC.
  const AC = window.AudioContext || window.webkitAudioContext;
  if (AC) {
    const origCss = AC.prototype.createMediaStreamSource;
    AC.prototype.createMediaStreamSource = function (stream) {
      attachStream(stream);
      return origCss.call(this, stream);
    };
  }
  try {
    const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
    if (desc && desc.set) {
      Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
        get: desc.get,
        set(v) { if (v) attachStream(v); desc.set.call(this, v); },
      });
    }
  } catch (e) { /* net definitora — ladno */ }

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

  // Vyachka VSEH nakoplennyh chanov (destruktivno): massiv ochishchaetsya,
  // vne shnit offset nie nuzhen — pishmo v fajl pridetaet porcyami.
  window.__mnemaDrain = function (offsetBytes) {
    const out = window.__mnemaChunks.map(function (c) { return c.b64; });
    window.__mnemaChunks = [];
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
