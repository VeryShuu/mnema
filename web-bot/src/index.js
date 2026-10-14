// Mnema web-bot: zapuskaet Zapisi vstrech Zoom cherez vebl-klient (FT-1..10, 41).
// Chitaet zadachi iz redis (mnema:launch), piyet statusy/sobytiya obratno v API.
// VNC-sessiya (kontsept "nastroyka + ekrany vstrech") — v vnc_manager.js.
import { createClient } from "redis";
import fs from "node:fs";
import path from "node:path";
import { launchAndRecord, healthCheckSession } from "./recorder.js";
import { salvageFromDisk } from "./salvage.js";
import { loadConfig } from "./config.js";
import { createLogger } from "./log.js";
import {
  VNC_QUEUE, setVncDeps, handleVncCommand, ensureSetup, startHeartbeat,
  startupCleanup, ensureJobVnc, releaseJobVnc,
} from "./vnc_manager.js";

const log = createLogger("web-bot");
const cfg = loadConfig();

const recorders = new Map();
const salvaging = new Set();

// Fayl-molotok na obshchem volume /data: watchdog api (maintenance_job)
// razlichaet «web-botumer» (molotok tikh) ot «web-bot zhiv, no job bez
// sobytiy» — vtoroy sluchay salvageNie nuzhen, zhivoy protsess sam
// razberyotsya (restart rekordera/vyhod po maximum), i ne sbityy s paniky.
function startHeartbeatFile() {
  const tick = () => {
    try { fs.writeFileSync(cfg.heartbeatFile, String(Date.now())); } catch { /* */ }
  };
  tick();
  setInterval(tick, 15000);
}

// Vosstanovlenie zadach Po syrym segmentam (syroty ubitogo protsessa ili
// prerval'noj finalize). `active`-rekordery nie tromaem.
async function handleSalvage(task) {
  const jobId = task.job_id;
  if (recorders.has(jobId) || salvaging.has(jobId)) return;
  salvaging.add(jobId);
  try {
    const ev = (e, d) => { postEvent(jobId, e, d).catch(() => {}); };
    const res = salvageFromDisk(jobId, cfg, ev);
    if (res.ok) {
      await postEvent(jobId, "recording_salvaged",
        res.already_finalized ? "запись уже была собрана — досылаю finished"
          : `собрано по сырым сегментам: ${res.duration_s ?? "?"} с`);
      await postFinished(jobId, res);
    } else if (res.fail_reason === "no_raw_segments") {
      await postStatus(jobId, "failed", "recording_lost",
        "ни сырых сегментов, ни финальной записи на диске нет");
    }
    // salvage_error: soobschaem — eto i reset taymera watchdog-a (sobytie v
    // zhurnale), i signal operatoru; povtornaya popitka vozmozhna pozhe
    if (!res.ok && res.fail_reason === "salvage_error") {
      await postEvent(jobId, "salvage_failed", res.error || "finalize ne davalsya");
    }
  } catch (e) {
    log.error(`salvage ${jobId}:`, e);
  } finally {
    salvaging.delete(jobId);
  }
}

// Zada chi za puskom: syryye segmenty bez final'nogo raw.webm = siroty
// predidushchogo zhiznennogo cikla containera. Sobiraem i dosylaem finished.
async function startupSalvageScan() {
  const jobsDir = path.join(cfg.dataDir, "jobs");
  let names = [];
  try { names = fs.readdirSync(jobsDir); } catch { return; }
  for (const name of names) {
    if (!/^\d+$/.test(name)) continue;
    const d = path.join(jobsDir, name);
    let files;
    try { files = fs.readdirSync(d); } catch { continue; }
    if (!files.some((f) => /^raw\.\d+_w\d+\.webm$/.test(f))) continue;
    if (files.includes("raw.webm")) continue;
    log.info(`salvage siroty pri starte: job ${name}`);
    await handleSalvage({ job_id: parseInt(name, 10), reason: "startup" });
  }
}

async function main() {
  log.info("start, poll", JSON.stringify({
    prejoin: cfg.prejoinSeconds, joinDeadlineMin: cfg.joinDeadlineMin,
    maxDurationMin: cfg.maxDurationMin, announce: cfg.announceWav,
    vncMaxSlots: cfg.vncMaxSlots,
  }));
  const redis = createClient({ url: cfg.redisUrl });
  redis.on("error", (e) => log.error("redis:", e.message));
  await redis.connect();
  setVncDeps(redis, cfg);
  startHeartbeatFile();
  await startupCleanup();
  startupSalvageScan().catch((e) => log.error("salvage scan:", e.message));
  try {
    await ensureSetup();
  } catch (e) {
    log.error("failed to bring up setup session:", e.message);
  }
  startHeartbeat();

  let processing = new Set();

  while (true) {
    try {
      // 1. novye zadachi starta
      const item = await redis.brPop("mnema:launch", 2);
      if (item) {
        const task = JSON.parse(item.element);
        if (!processing.has(task.job_id)) {
          processing.add(task.job_id);
          runTask(task, redis).finally(() => processing.delete(task.job_id));
        }
      }
      const stop = await redis.rPop("mnema:stop");
      if (stop) {
        const s = JSON.parse(stop);
        log.info("stop requested for", s.job_id, s.reason || "");
        recorders.get(s.job_id)?.requestStop(s.reason);
      }
      const salv = await redis.rPop("mnema:salvage");
      if (salv) {
        const s = JSON.parse(salv);
        log.info("salvage requested for", s.job_id, s.reason || "");
        handleSalvage(s).catch((e) => log.error("salvage:", e.message));
      }
      const vncCmd = await redis.rPop(VNC_QUEUE);
      if (vncCmd) {
        handleVncCommand(JSON.parse(vncCmd)).catch((e) =>
          log.error("vnc command:", e.message));
      }
      const hc = await redis.rPop("mnema:session_healthcheck");
      if (hc) {
        const ok = await healthCheckSession(cfg);
        await redis.lPush("mnema:session_health",
          JSON.stringify({ ok, ts: new Date().toISOString() }));
      }
    } catch (e) {
      log.error("loop error:", e.message);
      await sleep(3000);
    }
  }
}

async function runTask(task, redis) {
  const { job_id } = task;
  const recordersHolder = { requestStop: null };
  recorders.set(job_id, recordersHolder);
  try {
    await postStatus(job_id, "launching", null, null, 1);
    const onEvent = (ev, detail) => {
      postEvent(job_id, ev, detail).catch(() => {});
      if (ev === "joined") postStatus(job_id, "recording").catch(() => {});
    };
    const vnc = await ensureJobVnc(job_id);
    let run;
    try {
      run = await launchAndRecord(job_id, cfg, recordersHolder, onEvent, vnc);
    } finally {
      await releaseJobVnc(job_id);
    }
    if (run.ok) {
      await postFinished(job_id, run);
    } else if (run.fail_reason === "cancelled") {
      await postStatus(job_id, "cancelled", "manual_leave", run.error).catch(() => {});
    } else {
      await postStatus(job_id, "failed", run.fail_reason, run.error, null,
                       run.screenshot_path || null);
    }
  } catch (e) {
    log.error(`job ${job_id} task error:`, e);
    const reason = classifyFatal(e);
    // vozmozhno zapis' uzhe idla i chanki na diske — pytaemsya spasti to,
    // chto yest', prezhde chem soobschat' o probele
    try {
      const res = salvageFromDisk(job_id, cfg, (ev2, d2) =>
        postEvent(job_id, ev2, d2).catch(() => {}));
      if (res.ok) {
        await postEvent(job_id, "recording_salvaged",
          `задача упала (${reason}) — запись собрана из сохранённого`);
        await postFinished(job_id, res);
        return;
      }
    } catch { /* net syryyh — soobschaem failed kak byvalo */ }
    await postStatus(job_id, "failed", reason, String(e.message || e)).catch(() => {});
  } finally {
    recorders.delete(job_id);
  }
}

function classifyFatal(e) {
  const t = String(e.message || e).toLowerCase();
  if (t.includes("rtms") || t.includes("automated bot")) return "zoom_requires_rtms";
  if (t.includes("sign in") || t.includes("login")) return "auth_session_missing";
  return "page_crash";
}

// --- API klients -------------------------------------------------------------
// Retry s backoff: odin nezapisannyy finished = pотеряnnaya vstrecha naurovne
// orkestacii (job visit v recording do vechnosti), hotya dannye na diske.
// 4xx (409 nevalidnyy perekhod, 404 net zadachi) — nie retry: situaciya ne
// ispravit'sya povtorom.
const API = process.env.API_URL || "http://api:8000";
const API_RETRIES = 5;

async function apiPost(url, body) {
  let lastErr = "";
  for (let i = 0; i < API_RETRIES; i++) {
    try {
      const r = await fetch(url, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.ok) return { ok: true, status: r.status };
      const text = await r.text().catch(() => "");
      if (r.status >= 400 && r.status < 500) return { ok: false, status: r.status, text };
      lastErr = `${r.status}: ${text}`;
    } catch (e) {
      lastErr = String(e.message || e);
    }
    await sleep(Math.min(30000, 1000 * 2 ** i) + Math.random() * 500);
  }
  return { ok: false, status: 0, text: lastErr };
}

async function postEvent(jobId, event, detail) {
  const r = await apiPost(`${API}/api/jobs/${jobId}/events`,
    { event, detail: detail || "" });
  if (!r.ok) log.error(`event post ${jobId} -> ${r.status} ${r.text || ""}`);
}

async function postStatus(jobId, status, failReason, error, attempts, screenshotPath) {
  const r = await apiPost(`${API}/api/jobs/${jobId}/status`,
    { status, fail_reason: failReason || null, error: error || null,
      attempts: attempts ?? null, screenshot_path: screenshotPath || null });
  if (!r.ok) log.error(`status post ${jobId} -> ${r.status} ${r.text || ""}`);
}

async function postFinished(jobId, run) {
  const r = await apiPost(`${API}/api/jobs/${jobId}/finished`, {
    webm_path: run.webm, wav_path: run.wav || null, mp3_path: run.mp3 || null,
    duration_s: run.duration_s || null,
    sizes: run.sizes || {},
  });
  if (!r.ok) {
    log.error(`finished post ${jobId} -> ${r.status} ${r.text || ""} — zapis na diske, watchdog spasjot`);
    // signaliziruem o nevozmozhnosti soobschit o zavershenii — eto imenno tot
    // sluchay, kogda molotok+watchdog edinstvennyy put' k dannyh
    apiPost(`${API}/api/alert`, {
      subject: `Finished nie doшёл: job ${jobId}`,
      body: `web-bot ne smog soobschit' o zavershenii zapisi joba ${jobId} (${r.status} ${r.text || ""}). Fayly na diske; watchdog sparvyotsya, no proveryte.`,
      dedup_key: `finished_fail_${jobId}`,
    }).catch(() => {});
  }
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

main().catch((e) => { log.error(e); process.exit(1); });
