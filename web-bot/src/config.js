// Konfiguratsiya web-bot iz env (FT-37/38).
export function loadConfig() {
  return {
    redisUrl: process.env.REDIS_URL || "redis://redis:6379/0",
    zoomProfileDir: process.env.ZOOM_PROFILE_DIR || "/data/zoom-profile",
    dataDir: process.env.DATA_DIR || "/data",
    botName: process.env.BOT_NAME || "Mnema",
    urlTemplate: process.env.RECORDER_URL_TEMPLATE ||
      "https://app.zoom.us/wc/{meeting_id}/join?pwd={pwd}",
    prejoinSeconds: int(process.env.BOT_PREJOIN_SECONDS, 120),
    joinDeadlineMin: int(process.env.JOIN_DEADLINE_MIN, 10),
    silenceEndTimeoutMin: int(process.env.SILENCE_END_TIMEOUT_MIN, 5),
    maxDurationMin: int(process.env.BOT_MAX_DURATION_MIN, 360),
    pageReloadRetries: int(process.env.PAGE_RELOAD_RETRIES, 2),
    announceWav: process.env.ANNOUNCE_WAV || "templates/announce.wav",
    headful: process.env.HEADFUL === "1",
    captchaWaitMin: int(process.env.CAPTCHA_WAIT_MIN, 15),
    chunkFlushMs: int(process.env.CHUNK_FLUSH_MS, 5000),
  };
}

function int(v, d) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : d;
}

// Normalizatsiya ssylki-priglasheniya (FT-1).
export function buildJoinUrl(cfg, meetingId, pwd) {
  let url = cfg.urlTemplate.replace("{meeting_id}", meetingId);
  if (pwd) url = url.replace("{pwd}", pwd);
  else url = url.replace("?pwd=", "").replace("&pwd=", "");
  return url;
}
