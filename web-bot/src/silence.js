// Chiistyy detektor tişiny dlya avovyhoda iz sozwona (bez DOM/zavisimostey,
// testy: node --test). Period proveryayetsya ciklom waitForEnd; 5 minut —
// eto dlitelnost' nepreryvnoy tişiny, a nichtastota oprosa.
//
// levelDb — posledniy RMS uroven' sozwona v dBFS (null = izmerenie nedostupno:
// net payplayna/analyzera; v etom sluchae uroven' nie schitaetsya zvuchnym
// i nie sbrasyvayet taymer tishiny — za "nie v zone" otvechayet classifyPage).

export function isSoundLevel(levelDb, thresholdDb) {
  if (levelDb === null || levelDb === undefined || !Number.isFinite(levelDb)) return false;
  return levelDb > thresholdDb;
}

// Vozvraschaet { sound, lastSoundTs, silenceMin, graceDone, leave }.
// leave = proshyel greyys ot momenta vklyucheniya zapisi I zvuk otсутstvuyet
// nepreryvno >= silenceMs. Tişина mozhet nabegat' vo vremya greyysa — togda
// vyhod vozmožen syrazu po ego okonchanii.
export function decideSilence({ nowMs, joinedAtMs, lastSoundTsMs,
                                levelDb, thresholdDb, graceMs, silenceMs }) {
  const sound = isSoundLevel(levelDb, thresholdDb);
  const lastSoundTs = sound ? nowMs : lastSoundTsMs;
  const silenceMsActual = nowMs - lastSoundTs;
  const graceDone = nowMs - joinedAtMs >= graceMs;
  return {
    sound,
    lastSoundTs,
    silenceMin: silenceMsActual / 60000,
    graceDone,
    leave: graceDone && silenceMsActual >= silenceMs,
  };
}
