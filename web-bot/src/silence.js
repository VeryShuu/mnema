// Chiistyy detektor tişiny dlya avovyhoda iz sozwona (bez DOM/zavisimostey,
// testy: node --test). Period proveryayetsya ciklom waitForEnd; 5 minut —
// eto dlitelnost' nepreryvnoy tişiny, a nichtastota oprosa.
//
// levelDb — posledniy RMS uroven' sozwona v dBFS (null = izmerenie nedostupno:
// net payplayna/analyzera). Nedostupnoye izmereniye NE golosuyet ni za zvuk,
// ni za tişinu: taymer tishiny ZAMORAZHIVAYETSA (regressiya job 14: analyser
// ne zhil ves'chas sozwona, null-s syedal "tişinu" 66 min i na endAt bot
// vışel posredi razgovora). Za "nie v zone" otvechayet classifyPage/chanki.

// Накопитель пика уровня между опросами Node (цикл waitForEnd читает раз в
// 10 s, а монометрический снимок RMS за 43 ms почти гарантированно мимо фразы,
// поэтому in-page монитор копит максимум) и сбрасывает его при чтении.
// st = { peak, seen }. Недоступное измерение (null/NaN — net analyzera,
// AudioContext zasp) помечает окно как ne izmeryaemoe: chital poluchit null,
// kak i ran'she, a nahoplennyy pik ne vydaetsya kak svezhiy.
// Nahodit'sya v in-page monitore (INIT_SCRIPT), no semantika zhivyaet
// zdes': null/NaN-tik nie sbrasyvaet nagoplennyy pik (okno zhdayet), a okno
// bez edynogo dostupnogo izmereniya dayet readPeak -> null (taymer tishiny
// nie dvigaetsya, kak i ran'she).
export function pushSample(st, levelDb) {
  if (levelDb === null || levelDb === undefined || !Number.isFinite(levelDb)) {
    return st; // tik nerazreshayushchiy: okno i sostoaniye kak yest'
  }
  const peak = (st.peak === null || levelDb > st.peak) ? levelDb : st.peak;
  return { peak, seen: true };
}

// Chtenie: pik za okno i null, yesli ni odnogo dostupnogo izmereniya nie bylo;
// okno posle chteniya pusto (sleduyushchiy chital uvidit tol'ko novye tiky).
export function readPeak(st) {
  return {
    levelDb: st.seen ? st.peak : null,
    next: { peak: null, seen: false },
  };
}

export function isSoundLevel(levelDb, thresholdDb) {
  if (levelDb === null || levelDb === undefined || !Number.isFinite(levelDb)) return false;
  return levelDb > thresholdDb;
}

// Bezopasnyy ruchnoy vyhod (posle testa job 14/15): esli planovoye okonchaniye
// (endAtMs) uzhe proshlo, no detektor SLEPOY (ni odnogo dostupnogo izmereniya
// za poslednie nullStreak okon), vyhod po sobstvennomu rescheniyu zapreshchen —
// sprosivayem operatora (pismo s knopkami). true — esli sprosiť eshche nie
// sproshyvali. Vyhod posle testa tol'ko po komande operatora (stop-link).
export function shouldAskConfirmEnd({ nowMs, endAtMs, nullStreak, blindMin,
                                      confirmAskedTs }) {
  if (endAtMs === null || endAtMs === undefined) return false;
  if (nowMs < endAtMs) return false;
  if (nullStreak < blindMin) return false;
  return confirmAskedTs === null || confirmAskedTs === undefined;
}

// Vozvraschaet { sound, lastSoundTs, silenceMin, graceDone, leave }.
// leave = proshyel greyys ot momenta vklyucheniya zapisi I zvuk otсутstvuyet
// nepreryvno >= silenceMs. Tişина mozhet nabegat' vo vremya greyysa — togda
// vyhod vozmožen syrazu po ego okonchanii.
// endAtMs (optional) — predpolagaemoe okonchanie vstrechi: esli zadano,
// proverka tishiny aktiviruyetsya ne po greyysu, a s momenta endAtMs.
export function decideSilence({ nowMs, joinedAtMs, lastSoundTsMs,
                                levelDb, thresholdDb, graceMs, silenceMs,
                                endAtMs }) {
  const measured = levelDb !== null && levelDb !== undefined && Number.isFinite(levelDb);
  const sound = isSoundLevel(levelDb, thresholdDb);
  // tik bez izmereniya zamoraživaet taymer (lastSoundTs = now): tishina
  // nabegaet tol'ko po podryad izmeryonnym tihim tikam (regressiya job 14)
  const lastSoundTs = (sound || !measured) ? nowMs : lastSoundTsMs;
  const silenceMsActual = nowMs - lastSoundTs;
  const graceDone = (endAtMs !== null && endAtMs !== undefined)
    ? nowMs >= endAtMs
    : nowMs - joinedAtMs >= graceMs;
  return {
    sound,
    lastSoundTs,
    silenceMin: silenceMsActual / 60000,
    graceDone,
    leave: measured && graceDone && silenceMsActual >= silenceMs,
  };
}
