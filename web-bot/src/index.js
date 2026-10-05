// Mnema web-bot: zapuskaet Zapisi vstrech Zoom cherez vebl-klient (FT-1..10, 41).
// Chitaet zadachi iz redis (mnema:launch), piyet statusy/sobytiya obratno v API.
// VNC-sessiya (kontsept "nastroyka + ekrany vstrech") — v vnc_manager.js.
import { createClient } from "redis";
import { launchAndRecord, healthCheckSession } from "./recorder.js";
import { loadConfig } from "./config.js";
import {
  VNC_QUEUE, setVncDeps, handleVncCommand, ensureSetup, startHeartbeat,
  startupCleanup, ensureJobVnc, releaseJobVnc,
} from "./vnc_manager.js";

const cfg = loadConfig();

async function main() {
  console.log("[web-bot] start, poll", JSON.stringify({
    prejoin: cfg.prejoinSeconds, joinDeadlineMin: cfg.joinDeadlineMin,
    maxDurationMin: cfg.maxDurationMin, announce: cfg.announceWav,
    vncMaxSlots: cfg.vncMaxSlots,
  }));
  const redis = createClient({ url: cfg.redisUrl });
  redis.on("error", (e) => console.error("[redis]", e.message));
  await redis.connect();
  setVncDeps(redis, cfg);
  await startupCleanup();
  try {
    await ensureSetup();
  } catch (e) {
    console.error("[vnc] ne udalos' podnyat' setup-sessiyu:", e.message);
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
        console.log("[web-bot] stop requested for", s.job_id, s.reason || "");
        recorders.get(s.job_id)?.requestStop(s.reason);
      }
      const vncCmd = await redis.rPop(VNC_QUEUE);
      if (vncCmd) {
        handleVncCommand(JSON.parse(vncCmd)).catch((e) =>
          console.error("[vnc] komanda:", e.message));
      }
      const hc = await redis.rPop("mnema:session_healthcheck");
      if (hc) {
        const ok = await healthCheckSession(cfg);
        await redis.lPush("mnema:session_health",
          JSON.stringify({ ok, ts: new Date().toISOString() }));
      }
    } catch (e) {
      console.error("[web-bot] loop error:", e.message);
      await sleep(3000);
    }
  }
}

const recorders = new Map();

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
    console.error(`[job ${job_id}] task error:`, e);
    const reason = classifyFatal(e);
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
const API = process.env.API_URL || "http://api:8000";

async function postEvent(jobId, event, detail) {
  const r = await fetch(`${API}/api/jobs/${jobId}/events`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ event, detail: detail || "" }),
  });
  if (!r.ok) console.error(`event post ${jobId} -> ${r.status}`);
}

async function postStatus(jobId, status, failReason, error, attempts, screenshotPath) {
  const r = await fetch(`${API}/api/jobs/${jobId}/status`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ status, fail_reason: failReason || null,
                           error: error || null, attempts: attempts ?? null,
                           screenshot_path: screenshotPath || null }),
  });
  if (!r.ok) console.error(`status post ${jobId} -> ${r.status}: ${await r.text()}`);
}

async function postFinished(jobId, run) {
  const r = await fetch(`${API}/api/jobs/${jobId}/finished`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({
      webm_path: run.webm, wav_path: run.wav || null, mp3_path: run.mp3 || null,
      duration_s: run.duration_s || null,
      sizes: run.sizes || {},
    }),
  });
  if (!r.ok) console.error(`finished post ${jobId} -> ${r.status}: ${await r.text()}`);
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

main().catch((e) => { console.error(e); process.exit(1); });
