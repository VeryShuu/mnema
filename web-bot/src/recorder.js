// Zapis' vstrechi Zoom cherez vebl-klient (FT-1..10, FT-41).
// Tehnika po razdelu 3 TZ: pat RTCPeerConnection do navigacii, mikshirovanie
// udalennyh audiodorozhek v AudioContext, MediaRecorder(opus/webm), vykachivanie
// chankov kazhdye 5 s, proverka sostoyaniy stranitsy.
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { buildJoinUrl } from "./config.js";
import { decideSilence, shouldAskConfirmEnd } from "./silence.js";
import {
  END_CONTROL_SRC, LEAVE_SRCS, LEAVE_DIALOG_SRCS,
  MIC_CANDIDATE_SRC, MIC_MUTED_SRC, MIC_ACTIVE_SRC, MIC_MUTED_ICON_SRC,
  JOINED_DOM_SEL, NO_AUDIO_SEL, IN_MEETING_SRC, verdictFromSignals,
} from "./zoom_labels.js";
import { cleanSingletonLocks, freshProfileCopy, profilesRoot, publicVncPort } from "./vnc_manager.js";

// Fake-audio-device Chrome (fake-device-for-media-stream) generiruet
// periodicheskiye bip-y — ik slishat vse uchastniki vstrechi, i oni zhe
// popadayut v zapis'. Podminkaem fake-mikrofon bezmolvnym WAV-faylom.
function ensureSilenceWav(dataDir) {
  const p = path.join(dataDir, "silence_mic.wav");
  try {
    if (!fs.existsSync(p)) {
      execFileSync("ffmpeg", ["-y", "-f", "lavfi", "-i",
        "anullsrc=r=48000:cl=mono", "-t", "1", "-c:a", "pcm_s16le", p],
        { stdio: "pipe" });
    }
    return fs.statSync(p).size > 0 ? p : null;
  } catch (e) {
    console.error("[mic] ne udalos' sozdat' bezmolvnyy wav:", e.message);
    return null;
  }
}

// Health-check Zoom-сессии (FT-24): otkryt' app.zoom.us KOPIEY persistent-profile
// (zhivoy setup-brauzer derzhit bazovyy profil' — SingletonLock) i prowerdity,
// nie trebuyetsya li login (sessiya zhivaya ~2 nedeli). Vozvr. true ok.
export async function healthCheckSession(cfg) {
  const tmpDir = path.join(profilesRoot(), "healthcheck-tmp");
  let ctx;
  try {
    freshProfileCopy(cfg.zoomProfileDir, tmpDir);
    ctx = await chromium.launchPersistentContext(tmpDir, {
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
    try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch { /* net */ }
  }
}

export const EVENT = { // FT-9
  NAVIGATING: "navigating", PREJOIN: "prejoin", WAITING_ROOM: "waiting_room",
  JOINED: "joined", ENDED: "ended", BOT_WALL: "bot_wall", LOGIN_REQUIRED: "login_required",
  TRACK_ADDED: "track_added", TRACK_REMOVED: "track_removed",
  CHUNKS_SAVED: "chunks_saved", SILENCE: "silence", RELOAD: "reload", ERROR: "error",
};

// Скриншот страницы в момент сбоя: png на общем volume (/data), API заберёт
// путь из POST /status и вложит в письмо. Страница может быть мертва —
// любая ошибка гасится, возвращаем null (письмо уйдёт без картинки).
export async function captureFailureShot(page, jobId, cfg) {
  try {
    const dir = path.join(cfg.dataDir, "failure_screenshots");
    fs.mkdirSync(dir, { recursive: true });
    const p = path.join(dir, `job_${jobId}.png`);
    await page.screenshot({ path: p, type: "png" });
    return fs.existsSync(p) ? p : null;
  } catch {
    return null;
  }
}

// vnc — descriptor sessii menedzhera {display, profileDir, slot, wsPort} | null.
// Esl' est' — pishem headful na individual'nom ekrane (operator mozhet voyti
// cherez noVNC i proyti capchu); net — headless na kopii profilya (bazovyy
// derzhit zhivoy setup-brauzer, odnovremennyy launch na nom zablokirovan).
export async function launchAndRecord(jobId, cfg, holder, onEvent, vnc = null) {
  const jobDir = path.join(cfg.dataDir, "jobs", String(jobId));
  fs.mkdirSync(jobDir, { recursive: true });
  let profileDir;
  if (vnc) {
    profileDir = vnc.profileDir;
  } else {
    // Fallback (slotov net): otdel'nyy put' headless-<id>, chto ne peresechsya
    // so slotovym job-<id> — iначе `open` ekrana v seredine headless-zapisi
    // sdul by rabochiy profil' Chromium (freshProfileCopy -> rmSync).
    profileDir = path.join(profilesRoot(), `headless-${jobId}`);
    try {
      fs.mkdirSync(profilesRoot(), { recursive: true });
      freshProfileCopy(cfg.zoomProfileDir, profileDir);
    } catch (e) {
      console.error(`[job ${jobId}] kopirovaniye profilya:`, e.message);
      profileDir = cfg.zoomProfileDir; // degradaciya: legasiy obshchiy profil'
      cleanSingletonLocks(cfg.zoomProfileDir);
    }
  }
  const webmPath = path.join(jobDir, "raw.webm");
  const wavPath = path.join(jobDir, "meeting.wav");
  const mp3Path = path.join(jobDir, "meeting.mp3");

  // Fake-audio-device Chrome generiruet periodicheskiye bip-y (gromkiye shchelki
  // slishat' vse uchastniki). Zamenyaem ego bezmolvnyym faylom: Chrome
  // istsiklyayet ego po krugu kakmikrofornyy vzork.
  const silenceWav = ensureSilenceWav(cfg.dataDir);
  const fakeArgs = ["--use-fake-device-for-media-stream"];
  if (silenceWav) fakeArgs.push(`--use-file-for-fake-audio-capture=${silenceWav}`);

  const ctx = await chromium.launchPersistentContext(profileDir, {
    headless: !(vnc || cfg.headful),
    env: vnc ? { ...process.env, DISPLAY: vnc.display } : process.env,
    args: [
      ...fakeArgs,
      "--use-fake-ui-for-media-stream",
      "--autoplay-policy=no-user-gesture-required",
    ],
    permissions: ["microphone"],
    viewport: { width: 1280, height: 800 },
  });
  const page = await ctx.newPage();

  const state = {
    stopRequested: false, stopReason: null, reloads: 0, joinedAt: null,
    lastChunksTs: Date.now(), lastSoundTs: Date.now(),
    lastNavTs: Date.now(), chunksSaved: 0, vnc, endAtMs: null,
    jobId, confirmAskedTs: null,
  };
  holder.requestStop = (reason) => {
    state.stopRequested = true;
    state.stopReason = reason || "stop";
  };

  await page.addInitScript(INIT_SCRIPT);

  // --- vykachivanie chankov kazhdye N s (FT-1). Okonnyy protokol:
  //     okno 0 (do arma, privetstviye) VYTRASYVAETSA; kazhdyy sekhnny khlopnul
  //     okno W>=1 (freym x okno) pishetsya v raw.<n>_w<W>.webm epoha = Date.now
  //     real'nogo rec.start() (running AudioContext + anchor-blip 17.5k 8 ms v
  //     nachale). Na finalize okna sravnivayutsya adelay+amix — bez atrimov po
  //     lozhnym epoham, kotorye syedali nachalo fraz.
  let recording = false;
  const frameFiles = new Map(); // `${fr}|w<W>` -> {n, p, fd, bytes, epochMs}
  let frameSeq = 0;
  const ensureSegFor = (fr, w) => {
    const key = `${fr.url()}#${fr.name()}|w${w}`;
    let rec = frameFiles.get(key);
    if (!rec) {
      const n = frameSeq++;
      const p = path.join(jobDir, `raw.${n}_w${w}.webm`);
      rec = { n, key, p, fd: fs.openSync(p, "w"), bytes: 0 };
      frameFiles.set(key, rec);
    }
    return rec;
  };
  const flushTimer = setInterval(async () => {
    if (!recording) return;
    let gotAny = false;
    try {
      let tracks = 0;
      const statuses = await Promise.all(page.frames().map(async (fr) => {
        const st = await fr.evaluate(() => {
          // posle reload novyy kontekst nie armed — re-arm (okno 1 s novoy epohoy)
          if (window.__mnemaDrain && !window.__mnemaGlobalArm && window.__mnemaArm) {
            try { window.__mnemaArm(); } catch (e) { /* */ }
          }
          if (!window.__mnemaDrain) return null;
          const d = window.__mnemaDrain();
          const s = window.__mnemaStatus ? window.__mnemaStatus() : {};
          return { d, s };
        }).catch(() => null);
        return { fr, st };
      }));
      for (const { fr, st } of statuses) {
        if (!st || !st.d || !st.d.wins) continue;
        for (const [wStr, chunks] of Object.entries(st.d.wins)) {
          const w = parseInt(wStr, 10);
          if (!w || !chunks || !chunks.length) continue; // okno 0 (pre-arm) - v korzinku; w>=1: otdalnoe fail
          const rec = ensureSegFor(fr, w);
          if (rec.epochMs === undefined)
            rec.epochMs = (st.d.epochs && st.d.epochs[w]) || Date.now();
          const buf = Buffer.from(chunks.join(''), "base64");
          fs.writeSync(rec.fd, buf);
          rec.bytes += buf.length;
          state.chunksSaved += buf.length;
          gotAny = true;
        }
        if (st.s && st.s.tracks > tracks) tracks = st.s.tracks;
      }
      if (gotAny) state.lastChunksTs = Date.now();
      state.liveTracks = tracks;
      if (tracks > 0 && (flushTimer.counter = (flushTimer.counter || 0) + 1) % 6 === 0)
        onEvent(EVENT.TRACK_ADDED, `активных дорожек: ${tracks}`);
      if (gotAny && (flushTimer.saved = (flushTimer.saved || 0) + 1) % 12 === 0)
        onEvent(EVENT.CHUNKS_SAVED, `сохранено байт: ${state.chunksSaved}`);
    } catch (e) {
      onEvent(EVENT.ERROR, `flush: ${e.message}`);
    }
  }, cfg.chunkFlushMs);

  try {
    const attemptRes = await joinMeeting(page, cfg, state, onEvent, jobId);
    if (!attemptRes.ok) {
      const screenshot_path = await captureFailureShot(page, jobId, cfg);
      return { ok: false, fail_reason: attemptRes.reason, error: attemptRes.error,
               screenshot_path };
    }
    state.joinedAt = Date.now();
    // tuslyar skryt do dvizheniya myshi — bez etohto mikrofon/knopki NE vidimy UI
    await wakeUi(page, onEvent);
    // esli Zoom pokazal banner "Podeslat' audio" (audio ne srazu) —
    // podklyuchaem marshrut, sonstsa zapis' budet bez zvuka.
    await ensureAudioRoute(page, onEvent);

    // Audio-obyavlenie (FT-10) sygraet DO zapisi: pre-arm okno 0 v kazhdom
    // freyme vytrasyvaetsya; posleokonchaniya frazy __mnemaArm otkryvaet
    // okno 1 (nastoyashchaya zapis') s tochnoy epohoy rec.start().
    await playAnnouncement(page, cfg, onEvent);
    // privetstviye sygrano — vyknuchayem mikrofon, chtoby feyk-mikrofona
    // ne shumel v konferentsii do kontsa zapisi.
    await muteMicrophone(page, onEvent);
    for (const fr of page.frames()) {
      await fr.evaluate(() => {
        try { if (window.__mnemaArm) window.__mnemaArm(); } catch (e) { /* */ }
      }).catch(() => {});
    }
    recording = true;
    state.lastChunksTs = Date.now();
    state.lastSoundTs = Date.now();

    // uzhol konca vstrechi / tihi / hard-limit (FT-4)
    let end;
    try {
      end = await waitForEnd(page, cfg, state, onEvent);
    } catch (e) {
      onEvent(EVENT.ERROR, `наблюдение прервалось: ${e.message}`);
      // stranitsa/browser zakhrylis' v hoze zapisi — sohranyaem nakoopolnuyu zapis'
      end = { reason: "page_closed" };
    }

    // ostannie chanki iz vseh payplaynov (route po oknam, okno 0 v korzinku)
    for (const fr of page.frames()) {
      try {
        const d = await fr.evaluate(() =>
          window.__mnemaDrain ? window.__mnemaDrain() : null).catch(() => null);
        if (!d || !d.wins) continue;
        for (const [wStr, chunks] of Object.entries(d.wins)) {
          const w = parseInt(wStr, 10);
          if (!w || !chunks || !chunks.length) continue;
          const rec = ensureSegFor(fr, w);
          if (rec.epochMs === undefined)
            rec.epochMs = (d.epochs && d.epochs[w]) || Date.now();
          const buf = Buffer.from(chunks.join(''), "base64");
          fs.writeSync(rec.fd, buf);
          rec.bytes += buf.length;
        }
      } catch { /* stranitsa uzhe mohla utochn */ }
    }

    if (end.reason === "reload_exhausted") {
      const screenshot_path = await captureFailureShot(page, jobId, cfg);
      return { ok: false, fail_reason: "page_crash",
               error: "страница умерла после всех перезагрузок", webm: webmPath,
               screenshot_path };
    }
    const segs = [...frameFiles.values()];
    for (const rec of segs) { try { fs.closeSync(rec.fd); } catch { /* */ } }
    return finalize(jobId, segs, webmPath, wavPath, mp3Path, state, end);
  } finally {
    clearInterval(flushTimer);
    for (const rec of frameFiles.values()) { try { fs.closeSync(rec.fd); } catch { /* already */ } }
    await cleanupContext(ctx, jobId, cfg, onEvent);
    if (!vnc && profileDir !== cfg.zoomProfileDir) {
      // kopiya fallback-profilya bolshe nie nuzhna (menedzher slotov ne sozdovalsya)
      try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch { /* net */ }
    }
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
    if (!meta) return { ok: false, reason: "timeout", error: "нет меты встречи" };
    if (meta.end_at) {
      // naive-ISO iz SQLite bez offseta vsegda UTC: Date.parse bez Z prochyotal
      // by kak lokal'noe vremya
      const iso = /Z|[+-]\d{2}:?\d{2}$/.test(meta.end_at) ? meta.end_at : meta.end_at + "Z";
      const t = Date.parse(iso);
      if (Number.isFinite(t)) state.endAtMs = t;
    }
    joinUrl = joinUrlFromMeta(cfg, meta);
  }
  onEvent(EVENT.NAVIGATING, "переход по ссылке входа");

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
               error: "анти-бот-стена (только через приложение Zoom) на стороне ведущего" };
    }
    if (verdict === "login_required") {
      onEvent(EVENT.LOGIN_REQUIRED, "редирект на Sign in");
      return { ok: false, reason: "auth_session_missing",
               error: "сессия Zoom в профиле не живая" };
    }
    if (verdict === "waiting_room") {
      onEvent(EVENT.WAITING_ROOM, "ожидание ведущего");
      // host vypuskaet Mnema vuchnuyu; zhdём do deadline
      if (await waitForLeaveButton(page, deadlineMs, onEvent)) {
        onEvent(EVENT.JOINED, "из комнаты ожидания");
        return { ok: true };
      }
      return { ok: false, reason: "waiting_room_stuck",
               error: "ведущий не выпустил из комнаты ожидания" };
    }
    if (verdict === "meeting_ended") {
      return { ok: false, reason: "not_started", error: "встреча уже завершилась" };
    }
    if (verdict === "prejoin") {
      onEvent(EVENT.PREJOIN, "имя + Join");
      await fillNameAndJoin(page, cfg, onEvent);
      // reCAPTCHA: reshaet tol'ko chelovek (§12). Zhdём vruchnuyu prokhod
      // cherez noVNC (MAX CAPTCHA_WAIT_MIN), bez avtomatiki.
      if (await waitForLeaveOrCaptcha(page, deadlineMs, cfg, onEvent, state.vnc)) {
        onEvent(EVENT.JOINED, "с пред-join экрана");
        return { ok: true };
      }
      continue; // eshche raz proitiremovat i popytats'
    }
    if (verdict === "joined") {
      onEvent(EVENT.JOINED, "уже в зоне");
      return { ok: true };
    }
    // pending: vozmozhno my uje v zale, no tuslyar skryt — budit UI i
    // perklassificiryuem; esli 1.5 min bez izmeneniy — odin reload dlya strahovki
    if (await classifyJoined(page, onEvent)) {
      onEvent(EVENT.JOINED, "в зоне после пробуждения тулбара");
      return { ok: true };
    }
    if (Date.now() - state.lastNavTs > 90000) {
      onEvent(EVENT.RELOAD, "зависший pending — перезагрузка");
      navigated = false;
      state.lastNavTs = Date.now();
    }
  }
  const domHint = await dumpPageSummary(page);
  return { ok: false, reason: state.stopRequested ? "cancelled" : "not_started",
            error: (state.stopRequested ? "остановлено вручную"
                     : "дедлайн входа истёк") + (domHint ? ` | DOM: ${domHint}` : "") };
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

// Bezopasnyy vyhod: posle planovovo okonchaniya zvuk nie izmeryaetsya —
// sprosivayem operatora (API otlavit письмо: «Завершить» + VNC + skrinshot).
// Oshibka nie lomit zapis' — v nastupayushchem cikle probuem eshche raz.
async function askConfirmEnd(page, cfg, state, onEvent, reason) {
  const API = process.env.API_URL || "http://api:8000";
  state.confirmAskedTs = Date.now();
  const shot = await captureFailureShot(page, state.jobId, cfg);
  onEvent("confirm_end_requested", `${reason} — вопрос оператору (письмо)`);
  try {
    const r = await fetch(`${API}/api/jobs/${state.jobId}/confirm-end`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ reason, screenshot_path: shot || null }),
    });
    if (!r.ok) {
      state.confirmAskedTs = null; // ne došlo — povtorim v sleduyushchem cikla
      console.error(`confirm-end ${state.jobId} -> ${r.status}`);
    }
  } catch (e) {
    state.confirmAskedTs = null;
    console.error(`confirm-end ${state.jobId}:`, e.message);
  }
}

// Klassifikator sostoyaniya stranitsy po neskolkim priznakam (FT-4/12 'hristkost' DOM).
// Vebl-klient Zoom: /my/... i /j/... otkryvayut zaglushku s iframe
// (meetings/iframe.html), gde lezhat knopki «Join from browser»/pre-join.
//
// Vazhno (vstrecha 40): tuslyar sozwona skryt CSS-invisility (`display:none`
// do navyedeniya myshi), a u skrytykh elementov `innerText` pustoy i
// `offsetParent === null` — proby po text/vidimosti nichego ne nahodili, i
// vozl prishodil v vechnyy `pending` pri jivoj zapisi. Poetomy metku syem
// iz `aria-label`/`title`/`textContent` (onik rabotayut i dlya skrytykh
// nodov) plus strukturnye priznaki zala po `data-tid`/id, bez fil'tra vidimosti.
export async function classifyPage(page) {
  let info;
  try {
    info = {};
    const frames = page.frames();
    for (const fr of frames) {
      const loc = await fr.evaluate(({ endRe, micJoinSel, joinedSel, noAudioSel, inMeetRe }) => {
        const txt = document.body ? (document.body.textContent || "") : "";
        const has = (s) => txt.toLowerCase().includes(s.toLowerCase());
        // textContent (nie innerText): u skrytogo tuslyara innerText = ""
        const labelOf = (el) => (
          el.getAttribute("aria-label") || el.getAttribute("title")
          || el.textContent || ""
        ).trim().toLowerCase();
        const btns = [...document.querySelectorAll("button, a, [role=button]")]
          .map(labelOf).filter(Boolean);
        return {
          // Leave (obychnaya vstrecha) ili End (personal'naya komnata, PMN):
          leaveBtn: btns.some(t => new RegExp(endRe, "i").test(t))
                    || document.querySelector(joinedSel) !== null
                    || btns.some(t => new RegExp(inMeetRe, "i").test(t)),
          joinBrowser: btns.some(t => /join from browser|войти из браузера/.test(t)),
          join: btns.some(t => t === "join" || t === "присоединиться"),
          micJoin: !!document.querySelector(micJoinSel),
          noAudio: !!document.querySelector(noAudioSel),
          botWall: has("automated bots aren't allowed") || has("must use a zoom app") ||
                   has("join requires sign in"),
          signRequired: has("sign in to continue to zoom"),
          waitingRoom: has("please wait for the host to start this meeting") ||
                       has("waiting for the host"),
          ended: has("the meeting has ended") || has("this meeting has ended"),
          captcha: /recaptcha/i.test(document.location.href)
                   || document.querySelector("iframe[src*='recaptcha'], .grecaptcha-badge") !== null
                   || has("protected by recaptcha"),
        };
      }, {
        endRe: END_CONTROL_SRC, micJoinSel: "#join_with_audio_link, #mic-button",
        joinedSel: JOINED_DOM_SEL, noAudioSel: NO_AUDIO_SEL, inMeetRe: IN_MEETING_SRC,
      }).catch(() => null);
      if (!loc) continue;
      for (const k of Object.keys(loc)) info[k] = (info[k] || loc[k]);
    }
    if (!Object.keys(info).length) return "pending";
    return verdictFromSignals({
      signRequired: !!info.signRequired, botWall: !!info.botWall,
      ended: !!info.ended, leaveBtn: !!info.leaveBtn, captcha: !!info.captcha,
      waitingRoom: !!info.waitingRoom, joinBrowser: !!info.joinBrowser,
      join: !!info.join, micJoin: !!info.micJoin,
    });
  } catch (e) {
    return "pending";
  }
}

// Kratkiy DOM-snimok dlya diagnostiki upadeniy not_started (vstrecha 40):
// po N knopok na freym s nadpismy, chtoby bylo vidno na chem zastoporilsya vhod.
async function dumpPageSummary(page) {
  try {
    const parts = [];
    for (const fr of page.frames().slice(0, 6)) {
      const s = await fr.evaluate(() => {
        const labelOf = (el) => (el.getAttribute("aria-label")
          || el.getAttribute("title") || el.textContent || "").trim().slice(0, 30);
        const btns = [...document.querySelectorAll("button, a, [role=button]")]
          .map(labelOf).filter(Boolean);
        const uniq = [...new Set(btns)].slice(0, 12);
        return uniq.join(", ");
      }).catch(() => "ERR");
      parts.push(`${fr.url().slice(0, 40)} [${s}]`);
    }
    return (page.url().slice(0, 50) + " || " + parts.join(" || ")).slice(0, 900);
  } catch { return null; }
}

// Zoom skryvaet tuslyar/paneli, poka kursór nie dvigalsya nad oblast'yu sozwona
// (vstrecha 40: knopki est' v DOM, no UI ne obrisovan). Posylаем realistichnyy
// pokaz dvizheniya myshi cherez CDP — eto otnositel'no bezopasno: klikov net.
async function wakeUi(page, onEvent = () => {}) {
  try {
    const vp = page.viewportSize() || { width: 1280, height: 800 };
    const cx = Math.round(vp.width / 2), cy = Math.round(vp.height / 2);
    await page.mouse.move(cx - 120, cy - 60, { steps: 8 });
    await page.mouse.move(cx, cy + 40, { steps: 8 });
    await page.mouse.move(cx, vp.height - 60, { steps: 12 }); // k niznei paneli
    await page.waitForTimeout(700);
    onEvent("wake_ui", "разбудил тулбар движением мыши");
    return true;
  } catch (e) {
    onEvent(EVENT.ERROR, `wake_ui: ${e.message}`);
    return false;
  }
}

// «Ya v zale» s popytkoy razbudit' UI: svechenie pending byvaet tol'ko
// iz-za skrytogo tuslyara. Vozvr. true/esli verdict == joined.
async function classifyJoined(page, onEvent) {
  if (await classifyPage(page) === "joined") return true;
  await wakeUi(page, onEvent);
  return (await classifyPage(page)) === "joined";
}


// Knopku/pole iskhem vo VSEH freymah (vebl-klientZoom rizmesya po IFRAMEam).
// Src — stroke-regex (peredayetsya v getByRole v kacestve name re).
async function clickAnyFrame(page, nameRe, waitMs = 3000) {
  const re = nameRe instanceof RegExp ? nameRe : new RegExp(nameRe, "i");
  const until = Date.now() + waitMs;
  while (Date.now() < until) {
    for (const fr of page.frames()) {
      const btn = fr.getByRole("button", { name: re }).first();
      if (await btn.isVisible({ timeout: 500 }).catch(() => false)) {
        await btn.click({ timeout: 5000 }).catch(() => {});
        return true;
      }
      const link = fr.locator("a,div[role=button]").filter({ hasText: re }).first();
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

// Poslе vklyucheniya v sozwon Zoom mohet vykiput' banner v klucheniya audia
// (vstrecha 40): bez nego zvukovyy marshrut ne podklyuchen i zapis' pusta.
// Knopka zivet v glavnom freyme po `data-tid`, no ne vsegda vidima bez myshi.
async function ensureAudioRoute(page, onEvent) {
  try {
    if (await clickAnyFrame(page,
      /join( with)?( computer)? audio|войти с компьютер|add audio|добавить аудио/i, 4000)) {
      onEvent("audio_route", "подключил аудиомаршрут по баннеру");
      await page.waitForTimeout(2000);
      return true;
    }
  } catch (e) { /* net banner — normalno */ }
  return false;
}

async function fillNameAndJoin(page, cfg, onEvent) {
  try {
    await clickAnyFrame(page, /accept cookies/i, 2500);
    // ekran «Join meeting» -> «Join from browser»
    if (await clickAnyFrame(page, /join from browser|войти из браузера/i, 4000)) {
      onEvent(EVENT.PREJOIN, "clicked 'Join from browser'");
      await page.waitForTimeout(4000);
    }
    // pred-join: imya + Join
    await fillAnyFrame(page, cfg.botName, 8000);
    // Vazhno (etaon vstrecha 41): knopka Join perekryta NEVIDIMYM sloem
    // recaptcha-enterprize,Therefore playwright-hit-test ne propuskayet real'nyy
    // click (timeout), a predydushchiy kod syebral eto kak uspeh. Klikaem
    // dispatch-eventom napryamuyu; esli capcha interactive — ee proydyot
    // chelovek cherez noVNC (wait daze eto i predusmatrivaet).
    if (await clickJoinButton(page)) {
      onEvent(EVENT.PREJOIN, "clicked 'Join'");
      await page.waitForTimeout(3000);
    } else {
      onEvent(EVENT.ERROR, "кнопка Join не найдена/перкрыта — ждём VNC");
    }
    // vybor audiomarshruta (esli wyskakhyvaet okno)
    if (await clickAnyFrame(page, /join( with)?( computer)? audio|войти с компьютер/i, 8000)) {
      onEvent(EVENT.PREJOIN, "clicked 'Join with computer audio'");
    }
  } catch (e) {
    onEvent(EVENT.ERROR, `fillNameAndJoin: ${e.message}`);
  }
}

// Knopka Join v pred-join ekrane: prioyhaem button s tekstrom/aria "Join"
// v lyubom freyme, real'nyy click s Korotkim Taymautom, pri perekrytii —
// dispatch polnogo tsykla (etot obkhod tol'ko dlya etoy knopki: zdes' nam
// nuzhen sam fakt nati, a capcha-spasayet nas ot shpiona, a ne my ot neyo).
async function clickJoinButton(page) {
  const re = /^join$|присоединиться/i;
  const until = Date.now() + 10000;
  while (Date.now() < until) {
    for (const fr of page.frames()) {
      const btn = fr.getByRole("button", { name: re }).first();
      if (!(await btn.isVisible({ timeout: 400 }).catch(() => false))) continue;
      // Locator.setAttribute moget otsutstvoval' v starykh versiyakh Playwright —
      // marksyu cherez evaluate (est' v ehe versiy).
      await btn.evaluate((el) => el.setAttribute("data-mnema-join", "1"))
        .catch(() => {});
      let ok = false;
      try { await btn.click({ timeout: 2500 }); ok = true; }
      catch { /* perekryto recaptcha-sloem */ }
      if (!ok) {
        ok = await fr.evaluate(() => {
          let el = document.querySelector("[data-mnema-join]");
          if (!el) { // marka nie prilila — ishchem knopku sami po tekstu/aria
            el = [...document.querySelectorAll("button, a, [role=button]")].find((b) =>
              /^(join|присоединиться)$/i.test((b.getAttribute("aria-label")
                || b.textContent || "").trim()));
          }
          if (!el) return false;
          const r = el.getBoundingClientRect();
          const opts = { bubbles: true, cancelable: true, view: window,
            clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0 };
          for (const type of ["pointerover", "pointerdown", "pointerup",
                              "mouseover", "mousedown", "mouseup", "click"]) {
            const ev = type.startsWith("pointer")
              ? new PointerEvent(type, { ...opts, pointerId: 1, isPrimary: true })
              : new MouseEvent(type, opts);
            el.dispatchEvent(ev);
          }
          return true;
        }).catch(() => false);
      }
      await fr.evaluate(() => {
        const el = document.querySelector("[data-mnema-join]");
        if (el) el.removeAttribute("data-mnema-join");
      }).catch(() => {});
      if (ok) return true;
    }
    await page.waitForTimeout(1000);
  }
  return false;
}

async function waitForLeaveButton(page, deadlineMs, onEvent = () => {}) {
  while (Date.now() < deadlineMs) {
    if (await classifyJoined(page, onEvent)) return true;
    const v = await classifyPage(page);
    if (v === "bot_wall" || v === "login_required" || v === "meeting_ended") return false;
    await page.waitForTimeout(3000);
  }
  return false;
}

// reCAPTCHA na knopke Join reshaet tol'ko chelovek (§12, kadptcha). Zhdёm
// vruchnuyu prokhoda cherez individual'nyy noVNC vstrechi: signal + odnorazovyy
// alert so ssylkoy na ekrannyy slot, do CAPTCHA_WAIT_MIN.
let captchaAlerted = false;
async function waitForLeaveOrCaptcha(page, deadlineMs, cfg, onEvent, vnc = null) {
  const waitMs = (cfg.captchaWaitMin || 15) * 60 * 1000;
  const capUntil = Math.min(deadlineMs, Date.now() + waitMs);
  let sawCaptcha = false;
  while (Date.now() < capUntil) {
    const v = await classifyPage(page);
    if (v === "joined") return true;
    if (v === "pending" || v === "prejoin") {
      // tuslyar mog ostavatsya skrytym — budit UI i perklassifikiryuem
      if (await classifyJoined(page, () => {})) return true;
    }
    if (v === "captcha") {
      if (!sawCaptcha) {
        sawCaptcha = true;
        if (vnc) {
          const portHint = String(publicVncPort(cfg, vnc.slot));
          onEvent("captcha",
            `reCAPTCHA на Join — нужен ручной проход через noVNC :${portHint}`);
          if (!captchaAlerted) {
            captchaAlerted = true;
            alertOperator("reCAPTCHA при входе в встречу",
              `Откройте noVNC :${portHint} (Connect) и пройдите капчу в окне Zoom. ` +
              `Автоматизация капчи запрещена (§12).`);
          }
        } else {
          // Slotov net: vstrecha pishetsya headless, okna s kapchoy NIгде — na
          // setup-ekran (:8080) ee NET. Vslepu otpravlyat' operatora туда —
          // zhestoko srezat' osmyslennost' okna CAPTCHA_WAIT_MIN.
          onEvent("captcha",
            "reCAPTCHA на Join, но индивидуальный экран VNC нет (слотов не хватило" +
            " или отказ стека) — запись идёт headless, капчу пройти нечем");
          if (!captchaAlerted) {
            captchaAlerted = true;
            alertOperator("reCAPTCHA без VNC-экрана",
              "Встреча столкнулась с reCAPTCHA, но свободных VNC-слотов нет " +
              "(или не поднялся дисплей): запись идёт headless и продолжится " +
              "без успеха до истечения CAPTCHA_WAIT_MIN. Освободите слот / перезапустите " +
              "web-bot и загрузите встречу заново. Автоматизация капчи запрещена (§12).");
          }
        }
      }
    } else if (sawCaptcha && (v === "bot_wall" || v === "login_required" || v === "meeting_ended")) {
      return false;
    }
    await page.waitForTimeout(3000);
  }
  return false;
}

// ----------------------------------------------------- wyklyuchenie mikrofonа
// Knopka mikrofona v toolbare Zoom lezhit v glavenem freyme, no resincy/locales
// raznyye. V personal'noy komnate (PMN) eto <a role="button"> s aria-label
// "Mute my microphone" — ego ne lovil ni selector `nc-mic-btn`, ni obhod
// po `button[aria-label]` (vstrecha 39: 8x "klik ne podtverdirsya").
// Opredelyaem sostoyanie po dvum nezavisimym priznakam (aria-label i klass
// ikonki), klykaem IMENNO naydennyy element (marka data-mnema-mic + dispatch
// polnogo tsykla pointer-/mouse-sobytiy), bez toroga po chuzhim elementam.
async function micState(fr, requireVisible = true) {
  const r = await fr.evaluate(({ candRe, mutedRe, activeRe, iconRe, needVis }) => {
    const micRe = new RegExp(candRe, "i");
    const mutedLblRe = new RegExp(mutedRe, "i");
    const activeLblRe = new RegExp(activeRe, "i");
    const mutedIconRe = new RegExp(iconRe, "i");
    const sels = ['[data-tid="mute_mic_button"]', '#mute-microphone-button-text',
      '#mic-button', '.nc-mic-btn', "button[aria-label], a[aria-label], [role=button][aria-label]"];
    const labelOf = (el) => (el.getAttribute("aria-label")
      || el.getAttribute("title") || el.textContent || "").trim().toLowerCase();
    for (const sel of sels) {
      for (const el of document.querySelectorAll(sel)) {
        if (!(el instanceof HTMLElement)) continue;
        if (needVis && !el.offsetParent && !el.getBoundingClientRect().width) continue;
        const icon = el.querySelector('[class*="microphone"], [class*="audio"]');
        const cls = `${el.className} ${icon ? icon.className : ""}`.toLowerCase();
        const label = labelOf(el);
        if (!micRe.test(`${cls} ${label}`)) continue;
        const iconMuted = mutedIconRe.test(cls);
        const labelMuted = mutedLblRe.test(label);
        const labelActive = activeLblRe.test(label);
        const muted = labelMuted && !labelActive ? true
          : labelActive && !labelMuted ? false
          : iconMuted;
        el.setAttribute("data-mnema-mic", "1");
        return { found: true, muted, label: label.slice(0, 40) };
      }
    }
    return { found: false, muted: null, label: "" };
  }, {
    candRe: MIC_CANDIDATE_SRC, mutedRe: MIC_MUTED_SRC,
    activeRe: MIC_ACTIVE_SRC, iconRe: MIC_MUTED_ICON_SRC,
    needVis: requireVisible,
  }).catch(() => ({ found: false, muted: null, label: "" }));
  return r;
}

//dispatch polnogo tsykla na element:Zoom-spa reagiruet na pointerdown/up,
// prostoy .click()/el.click() po <a role=button> inogda ignoring.
async function clickMarkedElement(fr, attr) {
  const handle = await fr.$(`[${attr}]`).catch(() => null);
  if (!handle) return false;
  let ok = false;
  try {
    await handle.click({ timeout: 4000 });
    ok = true;
  } catch { /* ne stablyno/prehrysto — niskiy uroven' */ }
  if (!ok) {
    ok = await fr.evaluate((a) => {
      const el = document.querySelector(`[${a}]`);
      if (!el) return false;
      const r = el.getBoundingClientRect();
      const opts = { bubbles: true, cancelable: true, view: window,
        clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0 };
      for (const type of ["pointerover", "pointerenter", "pointerdown", "pointerup",
                          "pointerout", "mouseover", "mousedown", "mouseup", "click"]) {
        const ev = type.startsWith("pointer")
          ? new PointerEvent(type, { ...opts, pointerId: 1, isPrimary: true })
          : new MouseEvent(type, opts);
        el.dispatchEvent(ev);
      }
      return true;
    }, attr).catch(() => false);
  }
  await fr.evaluate((a) => {
    const el = document.querySelector(`[${a}]`);
    if (el) el.removeAttribute(a);
  }, attr).catch(() => {});
  return ok;
}

async function muteMicrophone(page, onEvent) {
  const until = Date.now() + 30000;
  let seenAny = false;
  while (Date.now() < until) {
    let tried = false;
    // skrytyy tuslyar: offsetParent === null — razbivayem ego mysh'yu
    // pered kazhdoym popytkom, a posle dvukh porazheniy iskhem vovse bez
    // fil'tra vidimosti (dispatch na skrytom uje ne rabotaet, no Zoom
    // mog prikryt' panel' CSS-klassom pri aktivnoy webkam-oblasti).
    await wakeUi(page);
    for (const fr of page.frames()) {
      const st = await micState(fr);
      if (st.found) { seenAny = true; } else continue;
      if (st.muted) {
        onEvent("mute", `микрофон уже выключен (${st.label})`);
        return true;
      }
      const clicked = await clickMarkedElement(fr, "data-mnema-mic");
      tried = true;
      await page.waitForTimeout(1500);
      const after = await micState(fr);
      if (after.found && after.muted) {
        onEvent("mute", "микрофон выключен после приветствия");
        return true;
      }
      onEvent("mute", `клик по микрофону не подтвердился (${after.label})` +
        `${clicked ? "" : " — элемент не кликается"} — повтор`);
      break; // probuyem vDrugoy raz, ne klykaem povtorno v etoy pass
    }
    if (!tried && seenAny) {
      // kandidaty yest', no ne vidimy: klykaem vslepu po pervomu naidennomu
      for (const fr of page.frames()) {
        const st = await micState(fr, false);
        if (!st.found) continue;
        if (st.muted) {
          onEvent("mute", `микрофон выключен (скрытый тулбар, ${st.label})`);
          return true;
        }
        const clicked = await clickMarkedElement(fr, "data-mnema-mic");
        await page.waitForTimeout(1500);
        const after = await micState(fr, false);
        if (after.found && after.muted) {
          onEvent("mute", "микрофон выключен по скрытой кнопке");
          return true;
        }
        onEvent("mute", `слепой клик не подтвердился (mic=${st.label}, ` +
          `klik=${clicked ? "да" : "нет"})`);
        break;
      }
    }
    await page.waitForTimeout(2000);
  }
  onEvent(EVENT.ERROR, "не удалось выключить микрофон");
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

// Dialog vtorm Shaga vykhoda («Leave meeting» / «End meeting for all»):
// obychnyy click do nego ne dokhvachyvaet (vstrecha 48/49: shag 1 'najden',
// no klik ne realizovalsya — Zoom-toggle reaguirut tol'ko na dispatch
// polnogo tsykla, kak mikrofon). Klykaem dispatch po elementu s metkoy.
async function dispatchClickByLabel(page, srcs, maxLabelLen = 40, waitMs = 12000) {
  const until = Date.now() + waitMs;
  while (Date.now() < until) {
    for (const fr of page.frames()) {
      const ok = await fr.evaluate(({ srcsStr, maxLen }) => {
        const regs = srcsStr.split("\n").map((s) => new RegExp(s, "i"));
        const labelOf = (el) => (el.getAttribute("aria-label")
          || el.getAttribute("title") || el.textContent || "").trim();
        const tags = ["button", "a", "[role=button]", "[role=menuitem]"];
        const cands = [];
        for (const sel of tags) {
          for (const el of document.querySelectorAll(sel)) {
            const t = labelOf(el);
            if (t && t.length <= maxLen && regs.some((r) => r.test(t))) cands.push(el);
          }
        }
        for (const el of cands) {
          const r = el.getBoundingClientRect();
          if (!r.width || !r.height) continue;
          const opts = { bubbles: true, cancelable: true, view: window,
            clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0 };
          for (const type of ["pointerover", "pointerenter", "pointerdown",
                              "pointerup", "mouseover", "mousedown", "mouseup", "click"]) {
            const ev = type.startsWith("pointer")
              ? new PointerEvent(type, { ...opts, pointerId: 1, isPrimary: true })
              : new MouseEvent(type, opts);
            el.dispatchEvent(ev);
          }
          return true;
        }
        return false;
      }, { srcsStr: srcs.join("\n"), maxLen: maxLabelLen }).catch(() => false);
      if (ok) return true;
      await page.waitForTimeout(1000);
    }
  }
  return false;
}

async function leaveMeetingGracefully(page, state, onEvent) {
  let leaveClicked = false;
  try {
    // shag 1: krasnaya knopka "Leave" (skrytyy tuslyar — snachala razbudit';
    // klik cherez dispatch: Zoom nie reagiruut na obychnyy click)
    await wakeUi(page);
    leaveClicked = await dispatchClickByLabel(page, LEAVE_SRCS, 12, 8000);
    onEvent("leaving", leaveClicked ? "нажата кнопка Leave/End (шаг 1/2)"
                                    : "кнопка Leave/End не найдена");
    if (leaveClicked) {
      // shag 2: vyskakhyvayet dialog s vtoroy knopkoy «Leave meeting»
      // (dlya hosta — «End meeting for all»)
      await page.waitForTimeout(1500);
      const confirmed = await dispatchClickByLabel(page, LEAVE_DIALOG_SRCS, 40, 12000);
      onEvent("leaving", confirmed ? "подтверждён выход в диалоге (шаг 2/2)"
                                   : "диалог не найден — вторая попытка шага 1");
      if (!confirmed) {
        leaveClicked = await dispatchClickByLabel(page, LEAVE_SRCS, 12, 4000);
        if (leaveClicked) {
          await page.waitForTimeout(1500);
          const c2 = await dispatchClickByLabel(page, LEAVE_DIALOG_SRCS, 40, 8000);
          onEvent("leaving", c2 ? "подтверждён выход со второй попытки"
                                : "диалог не подтверждён — закрываем окно");
        }
      }
    }
  } catch (e) {
    onEvent(EVENT.ERROR, `leave: ${e.message}`);
  }
  // zhём zatichiyu (recorder dosbiraet ostatnie chanki) do 10 s
  const quietUntil = Date.now() + 10000;
  while (Date.now() < quietUntil) {
    const silenceS = (Date.now() - state.lastChunksTs) / 1000;
    if (silenceS > 4) break;
    try { await page.waitForTimeout(1000); } catch { break; }
  }
}

// -------------------------------------------------------------- konets zapiši
async function waitForEnd(page, cfg, state, onEvent) {
  // OKON BEZ IZMURENIY posle endAt, pri kotoryh sprašivaem operatora (10 s tik)
  const CONFIRM_END_BLIND_WINDOWS = 9;
  const hardMs = cfg.maxDurationMin * 60 * 1000;
  const graceMs = cfg.silenceGraceMin * 60 * 1000;
  const silenceLimitMs = cfg.silenceEndTimeoutMin * 60 * 1000;
  // diagnostika (job 14): analyser mog molchat' ves' sozvon — leveled-okna s
  // izmereniyem i peredryvka bez nego vyvodyatsya v sobytie,chtoby incident
  // bil viden po logu dzhoby, a nie tol'ko po razboru "tişiny"
  let nullStreak = 0;
  let lastLevelDb = null;
  while (!state.stopRequested) {
    await page.waitForTimeout(10000);
    // prinyuditelny limit
    if (Date.now() - state.joinedAt > hardMs) {
      onEvent(EVENT.ENDED, "максимальная длительность");
      return { reason: "max_duration" };
    }
    if (state.stopRequested) break;
    const v = await classifyPage(page);
    if (v === "meeting_ended") {
      onEvent(EVENT.ENDED, "страница: встреча завершена");
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
        onEvent(EVENT.RELOAD, `перезагрузка #${state.reloads}`);
        await page.reload({ waitUntil: "domcontentloaded" }).catch(() => {});
        continue;
      }
    } else {
      state.reloads = 0; // strannitsa zhivaya
    }
    // uroven' zvuka sozwona (RMS dBFS) po vsem freymam: maks. iz dostupnyh;
    // posle greyysa bez zvuka >= silenceEndTimeoutMin podryad — avovyhod
    let levelDb = null;
    for (const fr of page.frames()) {
      const l = await fr.evaluate(() =>
        window.__mnemaLevel ? window.__mnemaLevel() : null).catch(() => null);
      if (l !== null && l !== undefined && (levelDb === null || l > levelDb)) levelDb = l;
    }
    if (levelDb === null) {
      nullStreak++;
      // 15 podryad okon bez edinnogo izmereniya (= 150 s) — analyser/ctx skoree
      // vsego umen; signalyum v log dzhoby (odno sobytie na epizod)
      if (nullStreak === 15) {
        onEvent(EVENT.SILENCE,
          `нет измерений уровня звука ${nullStreak * 10} с подряд — детектор слеп, автовыход по тишине заблокирован`);
      }
    } else {
      if (nullStreak >= 15) {
        onEvent(EVENT.SILENCE, `измерение уровня восстановлено (пик ${levelDb.toFixed(1)} дБ)`);
      }
      nullStreak = 0;
      lastLevelDb = levelDb;
    }
    const d = decideSilence({
      nowMs: Date.now(), joinedAtMs: state.joinedAt, lastSoundTsMs: state.lastSoundTs,
      levelDb, thresholdDb: cfg.silenceLevelDb, graceMs, silenceMs: silenceLimitMs,
      endAtMs: state.endAtMs,
    });
    state.lastSoundTs = d.lastSoundTs;
    // planovoye okonchaniye proshlo, no zvuk nie izmeryaetsya (slepoy detektor) —
    // nie vyhodim samii, a sprosivayem operatora (pismo: «Zavershit'» + VNC + shot)
    if (shouldAskConfirmEnd({ nowMs: Date.now(), endAtMs: state.endAtMs,
                              nullStreak, blindMin: CONFIRM_END_BLIND_WINDOWS,
                              confirmAskedTs: state.confirmAskedTs })) {
      await askConfirmEnd(page, cfg, state, onEvent,
        `уровень звука недоступен ${nullStreak * 10} с после планового окончания — непонятно, идёт ли совещание`);
    }
    if (d.leave) {
      onEvent(EVENT.ENDED,
        `тишина ${d.silenceMin.toFixed(1)} мин подряд (> ${cfg.silenceEndTimeoutMin}) — автовыход`);
      await leaveMeetingGracefully(page, state, onEvent);
      return { reason: "silent_leave" };
    }
    // tišina: esli chanki ne idut dol'še SILENCE_END_TIMEOUT — proverit str.
    const silenceMin = (Date.now() - state.lastChunksTs) / 60000;
    if (silenceMin > cfg.silenceEndTimeoutMin) {
      onEvent(EVENT.SILENCE, `без чанков ${silenceMin.toFixed(1)} мин`);
      const v2 = await classifyPage(page);
      if (v2 !== "joined") {
        onEvent(EVENT.ENDED, "тишина + не в зоне");
        return { reason: "silence_then_not_joined" };
      }
      state.lastChunksTs = Date.now(); // esli v zone — prodolzhaem zhidat'
    }
  }
  // Ruchnaya ostanovka: graceful-wyhod iz sozwona (Leave -> подтверждение),
  // dam vremya na poslednie chanki, posle chego finalize soberaet zapis'.
  if (state.stopReason === "manual_leave") {
    await leaveMeetingGracefully(page, state, onEvent);
    onEvent(EVENT.ENDED, "ручное завершение: выход из созвона");
    return { reason: "manual_leave" };
  }
  return { reason: "stopped" };
}

// ------------------------------------------------------------- finalize FT-6/41
// segs: [{p, bytes, epochMs}] — po odnomu webm-potoku na freym-payplayn;
// rekordery ne ostanavlivayutsya nichego, kogoда (migraciya trekov mezhdu
// freymami nie dayet propadov). Obshchiy khronometrazh — epochMs sozdaniya
// payplayna po chasu konteynera. Baza shkly — armEpochMs (momet po完成
// privetstviya): khvosty do nego (sam privetstviye/hold) otrezayutsya atrimom,
// boleye polozhennye segmenty sdvigayutsya adelay. Odin segament bez shvona —
// prosto kopiruem.
function finalize(jobId, segs, webmPath, wavPath, mp3Path, state, end) {
  let durationS = null;
  const real = segs.filter((s) => s.bytes > 0 && fs.existsSync(s.p)
    && fs.statSync(s.p).size > 0)
    .sort((a, b) => (a.epochMs || 0) - (b.epochMs || 0));
  const base = (real[0] && real[0].epochMs) || Date.now();
  try {
    if (real.length === 1) {
      fs.copyFileSync(real[0].p, webmPath);
    } else if (real.length >= 1) {
      mixSegments(real, base, webmPath);
    }
  } catch (e) {
    /* bolshe vsego potokov — beriem naibolee polnyy bez shkly */
    if (!fs.existsSync(webmPath) || fs.statSync(webmPath).size === 0) {
      const biggest = [...real].sort((a, b) => b.bytes - a.bytes)[0];
      if (biggest) { try { fs.copyFileSync(biggest.p, webmPath); } catch { /* */ } }
    }
  }

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
  for (const p of segs) { try { fs.rmSync(p.p, { force: true }); } catch { /* */ } }
  for (const [k, p] of [["webm", webmPath], ["wav", wavPath], ["mp3", mp3Path]])
    if (fs.existsSync(p)) sizes[k] = fs.statSync(p).size;
  return {
    ok: true, webm: webmPath, wav: wavOk ? wavPath : null,
    mp3: mp3Ok ? mp3Path : null, duration_s: durationS, sizes,
    end_reason: end.reason,
  };
}

// atrim do armEpoch + adelay posle nego + amix v odin webm (normalize=0 —
// bez padaniya levelya). Vse segmenty na odnoy globalnoy shkalе vremeni.
function mixSegments(real, base, outPath) {
  const args = ["-y"];
  for (const s of real) args.push("-i", s.p);
  const parts = [];
  const labels = [];
  real.forEach((s, i) => {
    const epoch = s.epochMs ?? base;
    const delayMs = Math.max(0, Math.round(epoch - base));
    parts.push(`[${i}:a]aresample=48000,adelay=${delayMs}|${delayMs}[a${i}]`);
    labels.push(`[a${i}]`);
  });
  const chain = labels.length === 1
    ? `${labels[0]}anull[out]`
    : `${labels.join("")}amix=inputs=${real.length}:duration=longest:normalize=0[out]`;
  parts.push(chain);
  execFileSync("ffmpeg", [...args,
    "-filter_complex", parts.join(";"),
    "-map", "[out]", "-c:a", "libopus", "-b:a", "64k", outPath],
  { stdio: "pipe", timeout: 600000 });
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
    onEvent(EVENT.ERROR, "announce.wav не найден — пропуск");
    return;
  }
  const b64 = fs.readFileSync(wavPath).toString("base64");
  // Obyavlenie dolzhno sygrat' ROVNO ODIN RAZ. Fraym s virtual-miksherom
  // (micMixer) poyavlyaetsya Togda, cda Zoom vyzval getUserMedia — zhdyom jego,
  // chtoby fraza ushla v konferentsiyu; povtornyye vstryevaniya other freymov
  // pogloshchayetsya flagom __mnemaAnnPlayed na storone stranitsy.
  const until = Date.now() + 60000;
  let fired = false, fallbackUsed = false;
  while (Date.now() < until && !fired) {
    for (const fr of page.frames()) {
      let res = null;
      try {
        res = await fr.evaluate(async ([data]) => {
          if (window.__mnemaAnnPlayed) return { played: false, already: true, mixer: !!(window.__mnemaStatus && window.__mnemaStatus().micMixer) };
          const st = window.__mnemaStatus ? window.__mnemaStatus() : null;
          if (!st || !st.micMixer) return null; // net virtual'nogo mikrofona — ne signalim
          return window.__mnemaPlayAnnouncement(data);
        }, [b64]).catch(() => null);
      } catch { continue; }
      if (!res) continue;
      fired = true;
      if (res.played) {
        onEvent("announce", res.viaMic
          ? "объявление сыграно через виртуальный микрофон"
          : "объявление сыграно, но микрофон не найден — слышно только в записи");
      } else if (res.already) {
        onEvent("announce", "объявление уже было сыграно ранее");
      }
      break;
    }
    if (fired) break;
    // 40 s bez mikshera: Zoom ne ispolzuet nahu gUM-dorozhku — sygraem hotya by v zapis'
    if (!fallbackUsed && Date.now() > until - 20000) {
      fallbackUsed = true;
      for (const fr of page.frames()) {
        const res = await fr.evaluate(async ([data]) =>
          (window.__mnemaPlayAnnouncement && !window.__mnemaAnnPlayed
            ? window.__mnemaPlayAnnouncement(data) : null), [b64]).catch(() => null);
        if (res) {
          fired = true;
          onEvent("announce", "виртуальный микрофон не найден — объявление только в записи");
          break;
        }
      }
    }
    await page.waitForTimeout(2000);
  }
  if (!fired) onEvent(EVENT.ERROR, "announce: ни в одном фрейме не сыграно");
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
  // Analizer na vyhode miksha vsekh udalennyh dorozhek: urov' zvuka sozwona
  // (RMS dBFS) dlya detektora tishiny. Obyavleniye mimo dest (_mnemaLocal) —
  // v uroven nie popadayet.
  let analyser = null, anaBuf = null;
  // Okonnyy rekord: okno 0 — pre-arm (privetstviye), po komande __mnemaArm —
  // okno 1, koteroye i yest' nastoyashchaya zapis'. Epoha okna — Date.now v
  // moment REAL'nogo rec.start() pri UZHE RUNNING AudioContext (ranshe epoha
  // fijirovalas' v moment sozdaniya payplayna: poka Zoom ne derzhal gUM,
  // AudioContext visel suspended, i atrim po lozhnoy epoke syedal nachalo frazy).
  let win = 0;
  const winEpochs = {};
  window.__mnemaGlobalArm = false;

  function startWin() {
    if (!ctx) return;
    if (ctx.state === 'suspended') { ctx.resume().then(startWin).catch(() => {}); return; }
    const begin = () => {
      win += 1;
      winEpochs[win] = Date.now();
      const w = win;
      rec = new MediaRecorder(dest.stream, { mimeType: 'audio/webm' });
      rec.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) {
          ev.data.arrayBuffer().then((ab) => {
            const u8 = new Uint8Array(ab);
            let bin = '';
            const slice = 0x8000;
            for (let i = 0; i < u8.length; i += slice)
              bin += String.fromCharCode.apply(null, u8.subarray(i, i + slice));
            window.__mnemaChunks.push({ b64: btoa(bin), len: u8.byteLength, w });
            window.__mnemaTotal += u8.byteLength;
          });
        }
      };
      rec.start(4000);
    };
    if (rec && rec.state === 'recording') {
      rec.onstop = begin;
      try { rec.stop(); } catch (e) { begin(); }
    } else begin();
  }

  window.__mnemaArm = function () {
    // arm odin raz na kontekst: esli uzhe pishem okno >=1 — nie pieriezazhyvat'
    if (window.__mnemaGlobalArm) return win;
    window.__mnemaGlobalArm = true;
    startWin();
    return win;
  };

  function ensurePipeline() {
    if (ctx) return;
    ctx = new AudioContext();
    dest = ctx.createMediaStreamDestination();
    try {
      analyser = ctx.createAnalyser();
      analyser.fftSize = 2048;
      anaBuf = new Float32Array(analyser.fftSize);
      dest.connect(analyser);
    } catch (e) { analyser = null; }
    if (window.__mnemaGlobalArm) startWin(); // payplayn sozdan posle arma (migraciya trekov)
  }

  // VIRTUAL-NYY MIKROFON: vyvod getUserMedia podmenyaemsya mikshu
  // [fake-mikrofon + obyavlenie], chtoby frazu slishat' uchastniki cherez
  // shtatnyi kanal mikrofona Zoom (replaceTrack send-track nie sрабатывал).
  // Obe audiodorozhki vyvoda (i fake-mikrofon s tishinoy, i miksh) ne idut v
  // zapis' (_mnemaLocal) — obyavlenie kladyom v zapis' otdelno pri igre.
  let micMixerDest = null;
  try {
    const md = navigator.mediaDevices;
    const wrap = (orig) => function (...a) {
      return orig.apply(md, a).then((s) =>
        new Promise((res) => {
          try {
            const inAudio = s.getAudioTracks();
            if (!inAudio.length) return res(s);
            ensurePipeline();
            if (ctx.state === 'suspended') ctx.resume().catch(() => {});
            const mix = ctx.createMediaStreamDestination();
            ctx.createMediaStreamSource(new MediaStream(inAudio)).connect(mix);
            for (const t of mix.stream.getAudioTracks()) t._mnemaLocal = true;
            for (const t of s.getVideoTracks()) t._mnemaLocal = true;
            micMixerDest = mix;
            return res(new MediaStream([...s.getVideoTracks(),
              ...mix.stream.getAudioTracks()]));
          } catch (e) { res(s); }
        }));
    };
    if (md && md.getUserMedia) md.getUserMedia = wrap(md.getUserMedia);
    if (md && md.getDisplayMedia) md.getDisplayMedia = wrap(md.getDisplayMedia);
  } catch (e) { /* net mediaDevices — ladno */ }

  function attachTrack(track) {
    if (track.kind !== 'audio' || track._mnemaHooked || track._mnemaLocal) return;
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

  // Zapis' audio idyot cherez WebAudio: lovimsya k sozdanii istochnikov
  // MediaStream v lyubom AudioContext (createMediaStreamSource), a takzhe
  // HTMLMediaElement.srcObject — eto lovit dorozhki davje bez sobytiy PC.
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
      if (ev.transceiver && ev.transceiver.direction === 'sendonly') return; // nashi ne pishem
      attachTrack(ev.track);
    });
    const origOnTrack = Object.getOwnPropertyDescriptor(OrigPC.prototype, 'ontrack');
    Object.defineProperty(pc, 'ontrack', {
      set(fn) { pc.addEventListener('track', fn); },
      get() { return null; },
    });
    return pc;
  };
  window.RTCPeerConnection.prototype = OrigPC.prototype;

  // Vyachka VSEH nakoplennyh chanov (destruktivno): massiv ochishchaetscha.
  // Vozvraschaet ih po oknam (w): okno 0 (pre-arm) Node vytrasyvaet,
  // kazhdoye okno >=1 pishetsya v svoy fail raw.<n>_<w>.webm.
  window.__mnemaDrain = function () {
    const out = {};
    for (const c of window.__mnemaChunks) {
      (out[c.w] = out[c.w] || []).push(c.b64);
    }
    window.__mnemaChunks = [];
    return { wins: out, epochs: winEpochs, total: window.__mnemaTotal, startMs: winEpochs[win] };
  };
  window.__mnemaStatus = function () {
    return { tracks: window.__mnemaTracks, bytes: window.__mnemaTotal,
             recording: rec && rec.state === 'recording',
             micMixer: !!micMixerDest, win, startMs: winEpochs[win] || 0 };
  };

  // Uroven zvuka sozwona na mikshe udalennyh dorozhek: RMS v dBFS
  // (-120..0). Net payplayna/analyzera — null (nie путать с tişinoy).
  // Monitoret tikajet kazhdye 100 ms i KOPIT MAKSIMUM do chteniya. Oprosyvaet
  // ego tsykl waitForEnd raz v 10 s: odinochnyy snimok 43 ms (fft 2048 pri
  // 48 kGts) lovit rech s vyeroytiny edv. protsentov (job 9: chas sozwona s
  // rech'yu davno "tišinu" 29.9 min — nik odnogo snimoka ne popal v frazu).
  // Semantika kak u pushSample/readPeak v silence.js (testy tam): tik bez
  // dostupnogo izmereniya nie sbrasyvaet pik, a oknoBezIzmereniy chital
  // poluchayet null (taymer tishiny nie dvigaetsya, kak ran'şe).
  let lvlSt = { peak: null, seen: false };
  function sampleLevel() {
    if (!analyser || !ctx || ctx.state !== 'running') return;
    try {
      analyser.getFloatTimeDomainData(anaBuf);
      let sum = 0;
      for (let i = 0; i < anaBuf.length; i++) sum += anaBuf[i] * anaBuf[i];
      const rms = Math.sqrt(sum / anaBuf.length);
      const db = rms <= 0 ? -120 : Math.max(-120, 20 * Math.log10(rms));
      lvlSt = (lvlSt.peak === null || db > lvlSt.peak) ? { peak: db, seen: true } : lvlSt;
    } catch (e) { /* pusty okno kak yest' */ }
  }
  setInterval(sampleLevel, 100);
  window.__mnemaLevel = function () {
    // sine-otik i vnutri chteniya: v skrytoy vkladke Chrome trevhklaet taymery
    // (inensive throttling), interval ne idyot — okno pusteelelo by i tişina
    // nabegala by pri zhivoy rech' (scenariy job 9). Snyomka po zaprosu ot
    // taymerov nie zavisit.
    sampleLevel();
    const out = lvlSt.seen ? lvlSt.peak : null;
    lvlSt = { peak: null, seen: false };
    return out;
  };

  // FT-10: odin raz vlit' wav v kanal mikrofona (micMixerDest -> Zoom ->
  // uchastniki). V NASHU zapis' nikuda ne ljem: zapis' otrezaetsya do kontsa
  // privetstviya (armEpoch) i privetstviye v nie nie dolzhno popadat'.
  // Povtornyye vyzy NE proigryvayut: flag __mnemaAnnPlayed. Gain-ramp 20 ms
  // chtoby start/stop nie davali shchelchkov amplitudoy.
  let annPlaying = false;
  window.__mnemaAnnPlayed = false;
  window.__mnemaPlayAnnouncement = async function (b64) {
    if (window.__mnemaAnnPlayed || annPlaying)
      return { played: false, already: window.__mnemaAnnPlayed, mixer: !!micMixerDest };
    ensurePipeline();
    if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (e) { /* */ } }
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const audioBuf = await ctx.decodeAudioData(bytes.buffer);

    annPlaying = true;
    window.__mnemaAnnPlayed = true;
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0, ctx.currentTime);
    if (micMixerDest) gain.connect(micMixerDest); // v konferentsiyu kak 'mikrofon'

    const srcNode = ctx.createBufferSource();
    srcNode.buffer = audioBuf;
    srcNode.connect(gain);
    const t0 = ctx.currentTime + 0.05;
    gain.gain.setValueAtTime(0, t0);
    gain.gain.linearRampToValueAtTime(1, t0 + 0.02);
    gain.gain.setValueAtTime(1, t0 + audioBuf.duration);
    gain.gain.linearRampToValueAtTime(0, t0 + audioBuf.duration + 0.02);
    srcNode.start(t0);
    await new Promise((res) => {
      srcNode.onended = res;
      setTimeout(res, (audioBuf.duration + 1) * 1000);
    });
    try { srcNode.disconnect(); gain.disconnect(); } catch (e) { /* */ }
    annPlaying = false;
    return { played: true, viaMic: !!micMixerDest, duration: audioBuf.duration };
  };
})();
`;
