// Menedzher VNC-sessiy (novyy kontsept):
//
//   * setup-sessiya (slot 0 = postoyannyy ekrana :99 / noVNC :6080 / host 8080):
//     zhivoy Chrome na bazovom profile cfg.zoomProfileDir s otkrytym Zoom.
//     Operator vkhodit v akkaunt cherez noVNC — pechenki i otpechatki ostayutsya
//     v bazovom profile. Kryh brauzera — avto-perezapusk (do 5 v chas).
//
//   * sessii vstrech (sloty 1..cfg.vncMaxSlots): pri starte zadachi manager
//     podnimayet svoy displey :99+slot (Xvfb + x11vnc :5900+slot + websockify
//     :6080+slot) i delayet svezhuyu kopiyu bazovogo profilya v
//     <dataDir>/zoom-profiles/job-<id>. Browser zapuskaet recorder
//     (launchPersistentContext, DISPLAY=sess.display) — pri reCAPTCHA operator
//     otkryvaet http://<host>:<portBase+slot>/vnc.html i prokhodit yeye v etom
//     okne. Po zavershenii zadachi slot osvobozaetsya, kopiya profilya sghishaetsya.
//
//   * Sostoyanie vseh sessiy — redis-hash `mnema:vnc_sessions`
//     (polya "setup" | "job-<id>", JSON); api chitaet ego i stroit ssylki UI.
//   * Komandy — redis-list `mnema:vnc`:
//     {action: setup_open | setup_restart | open | close, job_id?}.
//
//   * Vse izmeneniy sostoyaniya (podnyatie/Release slotov) idut cherez
//     peredachnoy zamok withLock: v yom zhe take mozhet byt' tol'ko odna
//     operaciya allocation — eto isklyuchaet TOCTOU dvuh `open` komand.
import { chromium } from "playwright";
import { spawn, execFileSync } from "node:child_process";
import net from "node:net";
import fs from "node:fs";
import path from "node:path";

export const VNC_QUEUE = "mnema:vnc";
const VNC_HASH = "mnema:vnc_sessions";
const SETUP_KEY = "setup";

const NOVNC_WEB = process.env.NOVNC_DIR || "/usr/share/novnc";
const ZOOM_HOME = "https://app.zoom.us/wc/home";

let _cfg = null;
let _redis = null;
const sessions = new Map(); // "setup" | "job-<id>" -> sess
let heartbeatTimer = null;
let vncPasswordFile = null;

export function setVncDeps(redis, cfg) { _redis = redis; _cfg = cfg; }
export function profilesRoot() {
  const dataDir = _cfg?.dataDir || process.env.DATA_DIR || "/data";
  return path.join(dataDir, "zoom-profiles");
}
// Vneshniy port noVNC dlya slota (docker public = vncPublicPortBase + slot).
export function publicVncPort(cfg, slot) {
  return (cfg?.vncPublicPortBase ?? _cfg?.vncPublicPortBase ?? 8080) + (slot ?? 0);
}
const keyFor = (jobId) => `job-${jobId}`;

// ------------------------------------------------------------------ peredachnoy zamok
let lockChain = Promise.resolve();
function withLock(fn) {
  const run = lockChain.then(fn, fn);
  lockChain = run.then(() => undefined, () => undefined);
  return run;
}

// -------------------------------------------------------------------- utily
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function nowIso() { return new Date().toISOString(); }

function portListening(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const s = net.connect({ port, host });
    const done = (v) => { s.destroy(); resolve(v); };
    s.setTimeout(1000);
    s.on("connect", () => done(true));
    s.on("error", () => done(false));
    s.on("timeout", () => done(false));
  });
}

async function waitPortOpen(port, timeoutMs) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await portListening(port)) return true;
    await sleep(400);
  }
  return false;
}

async function waitXSocket(displayNum, timeoutMs) {
  const sock = `/tmp/.X11-unix/X${displayNum}`;
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (fs.existsSync(sock)) return true;
    await sleep(300);
  }
  return false;
}

function spawnDaemon(args0, rest, opts = {}) {
  const p = spawn(args0, rest, { detached: true, stdio: "ignore", ...opts });
  p.on("error", (e) => console.error(`[vnc] spawn ${args0}:`, e.message));
  return p.pid;
}

function killTree(pid) {
  if (!pid) return;
  try { process.kill(-pid, "SIGTERM"); return; } catch { /* ne grupa — probuyem pid */ }
  try { process.kill(pid, "SIGTERM"); } catch { /* uzhe net */ }
}

function pkill(pattern) {
  try { execFileSync("pkill", ["-f", pattern], { stdio: "ignore" }); }
  catch { /* processov net — norm */ }
}

function pgrepCount(pattern) {
  try {
    const out = execFileSync("pgrep", ["-f", pattern],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    return out.split("\n").filter(Boolean).length;
  } catch { return 0; }
}

// pkill/pgrep -f matchITsya podstrokoy po vsey komandnoy stroke: bez yakorya
// "user-data-dir=/data/zoom-profile" ubival by brauzery vseh vstrech
// (/data/zoom-profiles/job-N), a "job-1" — vstrechi "job-12". Yakor' ( |$) —
// za znachением flaga vsegda sleduyushchiy argument (probeł) ili konec stroki.
const profileProcPattern = (profileDir) => `user-data-dir=${profileDir}( |$)`;

export function cleanSingletonLocks(profileDir) {
  for (const f of ["SingletonLock", "SingletonCookie", "SingletonSocket"]) {
    try { fs.rmSync(path.join(profileDir, f), { force: true }); } catch { /* net — norm */ }
  }
}

// Vsegda s nulya: pechenki/otpechatki bazovogo profilya, no bez keyshey
// (Login Data), keishes i ostatochnyh blokirovok — individual'naya «lichnost'»
// vstrechi ne poretsetsya s setup-brauzerom.
export function freshProfileCopy(srcDir, dstDir) {
  fs.rmSync(dstDir, { recursive: true, force: true });
  fs.mkdirSync(dstDir, { recursive: true });
  if (srcDir !== dstDir && fs.existsSync(srcDir)) {
    execFileSync("cp", ["-a", `${srcDir}/.`, dstDir], { stdio: "pipe" });
    fs.rmSync(path.join(dstDir, "Default", "Login Data"), { force: true });
    fs.rmSync(path.join(dstDir, "Default", "Login Data-journal"), { force: true });
    for (const junk of ["Default/Cache", "Default/Code Cache", "Default/GPUCache",
                        "GrShaderCache", "ShaderCache"]) {
      fs.rmSync(path.join(dstDir, junk), { recursive: true, force: true });
    }
  }
  cleanSingletonLocks(dstDir);
}

// --------------------------------------------------------- redis-hash sostoyaniya
async function readHash() {
  if (!_redis || !_redis.isReady) return {};
  try { return await _redis.hGetAll(VNC_HASH); }
  catch (e) { console.error("[vnc] hGetAll:", e.message); return {}; }
}

function parseEntry(raw) { try { return JSON.parse(raw); } catch { return null; } }

async function publish(sess, status, error = null) {
  sess.status = status;
  if (!_redis || !_redis.isReady) return;
  try {
    // public_port — istochnik istiny za js: api doveryaet emu (rezhim
    // VNC_PUBLIC_PORT_BASE v drugom konteynere uzhe ne raz'edetsya molcha)
    await _redis.hSet(VNC_HASH, sess.key, JSON.stringify({
      kind: sess.kind, job_id: sess.jobId ?? null, slot: sess.slot,
      display: sess.display, ws_port: sess.wsPort, profile: sess.profileDir,
      public_port: publicVncPort(_cfg, sess.slot),
      status, error, ts: nowIso(), started_ts: sess.startedTs || null,
    }));
  } catch (e) { console.error("[vnc] publish:", e.message); }
}

async function dropHashKey(key) {
  try { if (_redis && _redis.isReady) await _redis.hDel(VNC_HASH, key); } catch { /* */ }
}

// ------------------------------------------------------------- displeynyy stek
function x11vncArgs(sess) {
  const args = ["-display", `:${sess.displayNum}`, "-forever", "-shared",
    // rfb tol'ko na loopback: vneshniy dostup isklyuchitel'no cherez websockify
    "-localhost", "-rfbport", String(sess.rfbPort)];
  // §HT: parol' VNC (VNC_PASSWORD) — v shifrovannom rfbauth-file, ne v cmdline;
  // bez nego -nopw, no rfb vse rahno zakryt -localhost vyše
  if (_cfg.vncPassword && vncPasswordFile) args.push("-rfbauth", vncPasswordFile);
  else args.push("-nopw");
  return args;
}

function websockifyArgs(sess) {
  const bind = _cfg.vncBindAddr ? `${_cfg.vncBindAddr}:` : "";
  const args = [`${bind}${sess.wsPort}`, `localhost:${sess.rfbPort}`,
    "--web", NOVNC_WEB];
  return args;
}

async function ensureDisplayStack(sess) {
  if (!(await portListening(sess.rfbPort))) {
    try { fs.rmSync(`/tmp/.X${sess.displayNum}-lock`, { force: true }); } catch { /* */ }
    try { fs.rmSync(`/tmp/.X11-unix/X${sess.displayNum}`, { force: true }); } catch { /* */ }
    sess.xvfbPid = spawnDaemon("Xvfb",
      [`:${sess.displayNum}`, "-screen", "0", _cfg.vncScreen]);
    if (!(await waitXSocket(sess.displayNum, 15000))) {
      throw new Error(`Xvfb :${sess.displayNum} ne padyomsya`);
    }
    const args = x11vncArgs(sess);
    sess.x11vncPid = spawnDaemon("x11vnc", args);
    if (!(await waitPortOpen(sess.rfbPort, 15000))) throw new Error("x11vnc ne padyomsya");
  }
  if (!(await portListening(sess.wsPort))) {
    sess.wsPid = spawnDaemon("websockify", websockifyArgs(sess));
    if (!(await waitPortOpen(sess.wsPort, 10000))) throw new Error("websockify ne padyomsya");
  }
}

function teardownStack(sess) {
  killTree(sess.xvfbPid); killTree(sess.x11vncPid); killTree(sess.wsPid);
  // starye protsessy ot predidushchego zhiznennogo cikla (pidov net) — po cmdline
  pkill(`Xvfb :${sess.displayNum}( |$)`);
  pkill(`x11vnc -display :${sess.displayNum}( |$)`);
  // zanyatyy varianty: `websockify 6081 ...` i `websockify 0.0.0.0:6081 ...`
  pkill(`websockify (${sess.wsPort}|[^ ]*:${sess.wsPort})( |$)`);
}

// -------------------------------------------------------------- struktura sessii
function makeSess(key, kind, jobId, slot) {
  return {
    key, kind, jobId: jobId ?? null, slot, status: "allocating",
    displayNum: 99 + slot, display: `:${99 + slot}`,
    rfbPort: 5900 + slot, wsPort: 6080 + slot,
    profileDir: kind === SETUP_KEY ? _cfg.zoomProfileDir
      : path.join(profilesRoot(), `job-${jobId}`),
    browserPid: null, xvfbPid: null, x11vncPid: null, wsPid: null,
    startedTs: null, restarts: 0, lastRestartTs: 0,
  };
}

function browserAlive(profileDir) {
  return pgrepCount(profileProcPattern(profileDir)) > 0;
}

function launchSetupBrowser(sess) {
  cleanSingletonLocks(sess.profileDir);
  const pid = spawnDaemon(chromium.executablePath(), [
    `--user-data-dir=${sess.profileDir}`,
    "--no-first-run", "--no-default-browser-check", "--no-sandbox",
    "--disable-dev-shm-usage", "--start-maximized",
    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    ZOOM_HOME,
  ], { env: { ...process.env, DISPLAY: sess.display } });
  if (!pid) throw new Error("ne udalos' zapustit' Chrome setup-sessii");
  sess.browserPid = pid;
}

// ------------------------------------------------------------------ setup
async function ensureSetupInner() {
  const cur = sessions.get(SETUP_KEY);
  if (cur && browserAlive(cur.profileDir)) return cur;
  await stopSessionInner(SETUP_KEY);
  const sess = makeSess(SETUP_KEY, SETUP_KEY, null, 0);
  sessions.set(SETUP_KEY, sess); // rezerwaciya do pervogo Await
  try {
    await ensureDisplayStack(sess);
    await publish(sess, "starting");
    launchSetupBrowser(sess);
  } catch (e) {
    sessions.delete(SETUP_KEY);
    teardownStack(sess);
    await publish(sess, "failed", e.message);
    throw e;
  }
  sess.startedTs = nowIso();
  await publish(sess, "running");
  console.log(`[vnc] setup: ${sess.display}, profil' ${sess.profileDir}`);
  return sess;
}

export function ensureSetup() { return withLock(ensureSetupInner); }

// Ulybaem tol'ko brauzer setup-ekrana (displey stoyt na meste), zatym
// podnyimaem zhivoy vnov'. Smyachem "browser umer" so smennoy ssylki UI.
export async function restartSetup() {
  return withLock(async () => {
    const cur = sessions.get(SETUP_KEY);
    if (cur) pkill(profileProcPattern(cur.profileDir));
    return ensureSetupInner();
  });
}

// ---------------------------------------------------------------- job-sessii
async function pickFreeSlot() {
  const used = new Set();
  const hash = await readHash();
  for (const [k, raw] of Object.entries(hash)) {
    if (!k.startsWith("job-")) continue;
    const v = parseEntry(raw);
    if (v && Number.isInteger(v.slot)) {
      // zhivy li profil' — esli net, sled staroy zhizni, slot svoboden
      if (v.profile && browserAlive(v.profile)) used.add(v.slot);
    }
  }
  for (const s of sessions.values()) if (s.kind === "job") used.add(s.slot);
  for (let s = 1; s <= _cfg.vncMaxSlots; s++) if (!used.has(s)) return s;
  return null;
}

async function ensureJobVncInner(jobId) {
  const key = keyFor(jobId);
  const live = sessions.get(key);
  if (live && live.status !== "failed") return live;
  if (live) { // perezapusk ne udavsheysya sessii: ulybaem sled i slot
    sessions.delete(key);
    await dropHashKey(key);
  }
  const slot = await pickFreeSlot();
  if (slot == null) {
    console.error(`[vnc] net svobodnyh slotov dlya job ${jobId} (макс ${_cfg.vncMaxSlots})`);
    return null;
  }
  const sess = makeSess(key, "job", jobId, slot);
  sessions.set(key, sess); // rezerwaciya synchronno pered long-Await
  try {
    await ensureDisplayStack(sess);
    fs.mkdirSync(profilesRoot(), { recursive: true });
    freshProfileCopy(_cfg.zoomProfileDir, sess.profileDir);
    sess.startedTs = nowIso();
    await publish(sess, "ready");
    console.log(`[vnc] job ${jobId}: slot ${slot}, ${sess.display}, profil' ${sess.profileDir}`);
    return sess;
  } catch (e) {
    console.error(`[vnc] ensureJobVnc(${jobId}):`, e.message);
    sessions.delete(key);
    teardownStack(sess);
    await dropHashKey(key);
    return null;
  }
}

// Vozvrat: sess {display, profileDir, wsPort, slot} | null (net slotov /
// otkaz steka). Browser NE zapuskaet — ego zapuskaet recorder.
export function ensureJobVnc(jobId) { return withLock(() => ensureJobVncInner(jobId)); }

// Polnoye osvobozhdenie: strakhovoy pkill brauzera (yakoryannyy), stek displeya,
// kopiya profilya.
async function releaseJobVncInner(jobId) {
  const key = keyFor(jobId);
  await stopSessionInner(key);
  await dropHashKey(key);
  try {
    fs.rmSync(path.join(profilesRoot(), `job-${jobId}`),
      { recursive: true, force: true });
  } catch { /* net */ }
}

export function releaseJobVnc(jobId) {
  return withLock(() => releaseJobVncInner(jobId));
}

async function stopSessionInner(key) {
  const sess = sessions.get(key);
  sessions.delete(key);
  if (!sess) return;
  pkill(profileProcPattern(sess.profileDir));
  killTree(sess.browserPid);
  if (sess.kind === "job") teardownStack(sess);
}

// -------------------------------------------------------------- komandy ocheredi
export async function handleVncCommand(msg) {
  const { action, job_id: jobId } = msg || {};
  if (action === "setup_open") { await ensureSetup(); return; }
  if (action === "setup_restart") { await restartSetup(); return; }
  if (jobId == null) return;
  if (action === "open") { await ensureJobVnc(jobId); return; }
  if (action === "close") { await releaseJobVnc(jobId); return; }
  console.warn("[vnc] neizvestnoye deystvie:", action);
}

// --------------------------------------------------------------- zhivyost'
export function startHeartbeat() {
  if (heartbeatTimer) return;
  heartbeatTimer = setInterval(() => {
    if (heartbeatBusy) return;
    heartbeatBusy = true;
    heartbeatTick().catch((e) => console.error("[vnc] heartbeat:", e.message))
      .finally(() => { heartbeatBusy = false; });
  }, 15000);
}
let heartbeatBusy = false;

// Zhivyost' session + GC: slot osvobozaetsya, kogda browser vstrechi umer
// (recorder zavershil / upal) prodlya minuty; ekrany, sozdannye cherez API, no
// tak i ne ispol'zovannye (nikto ne zapuskal browser), zhivut 15 min; zhestkiy
// cap — 6 chasov. Eshchyo — revizii re-publisha: esli zapisi v redis-hash
// propali (sboy redis, FLUSHALL), no sessii zhivy — vnov' publikuem, inache
// panel' vechno pokaazyvaet «nie sozdana».
const JOB_DEAD_GRACE_MS = 60000;
const JOB_READY_TTL_MS = 15 * 60000;
const JOB_MAX_LIFE_MS = 6 * 3600000;

// Esli zapisi v redis propali (sboy/FLUSHALL), no sessii zhivy — publikuem
// zanovo, inache panel' vechno pokaazyvaet «nie sozdana».
async function republishMissing() {
  if (!_redis || !_redis.isReady) return;
  const hash = await readHash();
  for (const [key, sess] of sessions) {
    if (hash[key]) continue;
    await publish(sess, sess.status || "starting");
  }
}

async function heartbeatTickInner() {
  await republishMissing();
  const setup = sessions.get(SETUP_KEY);
  if (!setup) {
    // esli pri starte setup sie ne podyalsya (net redis / Xvfb) — probуyet' snova
    try { await ensureSetupInner(); } catch { /* perezaprobuem v sleduyushchem take */ }
  } else if (!browserAlive(setup.profileDir)) {
    if (Date.now() - setup.lastRestartTs > 3600000) setup.restarts = 0;
    if (setup.restarts < 5) {
      setup.restarts += 1;
      setup.lastRestartTs = Date.now();
      console.log(`[vnc] setup-brauzer umer; perezapusk #${setup.restarts}`);
      try {
        launchSetupBrowser(setup);
        await publish(setup, "running");
      } catch (e) {
        await publish(setup, "failed", e.message);
      }
    } else {
      await publish(setup, "failed", "setup-brauzer umiraet povtorno >5 raz v chas");
    }
  }
  for (const [key, sess] of [...sessions]) {
    if (key === SETUP_KEY) continue;
    if (browserAlive(sess.profileDir)) {
      sess.everAlive = true;
      sess.deadSince = 0;
      if (sess.status !== "running") await publish(sess, "running");
      continue;
    }
    sess.deadSince = sess.deadSince || Date.now();
    const bornTs = sess.startedTs ? Date.parse(sess.startedTs) : 0;
    const readyUnused = !sess.everAlive && (Date.now() - bornTs > JOB_READY_TTL_MS);
    const stale = readyUnused ||
      (sess.everAlive && Date.now() - sess.deadSince > JOB_DEAD_GRACE_MS) ||
      (bornTs && Date.now() - bornTs > JOB_MAX_LIFE_MS);
    if (stale) {
      console.log(`[vnc] job ${sess.jobId}: slot ${sess.slot} osvobozhdayetsya`);
      await releaseJobVncInner(sess.jobId);
    }
  }
}

export function heartbeatTick() { return withLock(heartbeatTickInner); }

// Start konteynera: sledy ot predidushchogo zhiznennogo cikla ne znachimy —
// job-streki ulybayemsya. Eshchyo — preduprezhdenie o ne sovmestimoj
// publikatsiey portov (compose publishit 8080-8100:6080-6100 po umolchaniyu).
export async function startupCleanup() {
  if (_cfg.vncPublicPortBase !== 8080 || _cfg.vncMaxSlots > 20) {
    console.warn("[vnc] VNC_PUBLIC_PORT_BASE/VNC_MAX_SLOTS otlichayutsya ot defolta — " +
      "ublikovannyy v docker-compose diapazon (8080-8100:6080-6100, env " +
      "VNC_PUBLISHED_RANGE) dolzhen im sootvetstvovat', inache ssylki budut " +
      "vesti na nie publikuemyye porty");
  }
  if (_cfg.vncPassword) {
    // x11vnc proshit parol' v shifrovannom vide iz faila; ne pishem v ps/cmdline
    try {
      vncPasswordFile = "/tmp/mnema-vnc.passwd";
      execFileSync("x11vnc", ["-storepasswd", _cfg.vncPassword, vncPasswordFile],
        { stdio: "pipe" });
      fs.chmodSync(vncPasswordFile, 0o600);
    } catch (e) {
      console.error("[vnc] ne udalos' sohranit' parol':", e.message);
      vncPasswordFile = null;
    }
  }
  const hash = await readHash();
  for (const key of Object.keys(hash)) {
    if (key === SETUP_KEY || !sessions.has(key)) {
      const v = parseEntry(hash[key]);
      if (key === SETUP_KEY && v && v.profile && browserAlive(v.profile)) continue;
      await dropHashKey(key);
    }
  }
  for (const [key] of [...sessions]) {
    if (key !== SETUP_KEY && !browserAlive(sessions.get(key).profileDir)) {
      await stopSessionInner(key);
    }
  }
}
