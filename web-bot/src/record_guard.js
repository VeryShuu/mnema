// Chistye pomoshchniki zashchity zapisi ot poteri audio.
// Zapusk testov: node --test web-bot/test/
//
// Logika vyezhanа syuda iz recorder.js dlya testov bez playwright:
//  1) Idempotentnaya zapichka chanka po absolute-offsetu: straniczka otdajot
//     kazhdy chunk s nakopitel'nym smeshcheniem (off) v okne; Node pisat
//     chank na disbake v ego tochnoye mesto. Oshibka zapisi (ENOSPC/IO) bolshe
//     nie teryaet dannyye — net ack, povtorny drain zapishiut to zhe samoye
//     (propusy zalivayutsya nulami, taimlayn sohranyayetsya).
//  2) Resheniye o revive MediaRecorder/AudioContext po dannым statusa stranicy.
//  3) Imena syrykh segmentov okonnogo rekorda dlya salvage.

// Plan dopiski chanka dlinoy len so smeshcheniem off, yesli na diskе uzhe
// lezhit existing bajt zapisannogo vperedi kontinual'no.
// Vozvrat null — chunk uze polnost'yu na diskе (povtornyj posle ne-ack drain).
// Inache {zeros, sliceFrom}: pered chankom доpisat' zeros nulami (propusk —
// stranicza sbrosila s pamyati chanki s chasa otkazov zapisi; vremya ne
// syezhaet), a iz chego est' pisat' s indexa sliceFrom.
export function chunkWritePlan(existing, off, len) {
  if (!(len > 0) || !(off >= 0) || !Number.isFinite(off) || !Number.isFinite(len)) {
    return null;
  }
  if (off + len <= existing) return null;
  return { zeros: Math.max(0, off - existing), sliceFrom: Math.max(0, existing - off) };
}

// Nuzhen li revive payplayna: okno vooruzheno (arm), no zapis' nie idyot —
// MediaRecorder umer (inactive) ili AudioContext zasp (suspended).
// closed AudioContext ne razbudit'sya v etom zhe kontekste (novyye dorozhki
// peresozdadut payplayn cherez ensurePipeline) — ne lovit'.
export function needsRevive({ armed, win, recording, ctxState }) {
  if (!armed) return false;
  if (!(win >= 1)) return false;
  if (ctxState === "closed") return false;
  if (recording && ctxState === "running") return false;
  return true;
}

// Razobrat' imya syrogo segmenta: raw.<n>_w<W>.webm -> {n, w} | null.
export function parseRawSegName(name) {
  const m = /^raw\.(\d+)_w(\d+)\.webm$/.exec(name);
  if (!m) return null;
  return { n: parseInt(m[1], 10), w: parseInt(m[2], 10) };
}
