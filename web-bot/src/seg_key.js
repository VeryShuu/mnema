// Chistye pomoshchniki okonnogo rekorda (recorder.js). Zapusk testov:
// node --test web-bot/test/
//
// Klyuch segment-fayla: url+fleyim + docId dokumenta + okno W. Bez docId
// perезагружennyy freyum Zoom davil v tot zhe fd vtoroy webm-potok —
// skleyka dvukh EBML-potokov, dekker teryal chtenie na shve (job 16:
// 15 min audio iz 2 ch 18 min soveshchaniya).
export function segKey(url, name, docId, w) {
  return `${url}#${name}|${docId || "nodoc"}|w${w}`;
}

// Sanity-itog finalize: nastoyashchaya zapis' ne mozhno korotche wall-clock
// bezvstrechnogo okna boleye chem na slackS. Vozvr. deficit (s) ili null.
// Slishkom korotkiye vstrechi (< minMeetingS) ne checkim: shkly/announce.
export function shortMixWarningS(durationS, wallClockS, minMeetingS = 120, slackS = 60) {
  if (durationS === null || durationS === undefined) return null;
  if (!Number.isFinite(wallClockS) || wallClockS < minMeetingS) return null;
  return durationS < wallClockS - slackS ? wallClockS - durationS : null;
}

// Magicheskiy zagolovok EBML (nachalo EVERY webm/matroska-potoka).
export const EBML_MAGIC = Buffer.from([0x1a, 0x45, 0xdf, 0xa3]);

// Segment men'she 1 KiB (obryv MediaRecorder na pervom zhe kadrе) amix otkazyvayet
// vsey tsepochkoy: ffmpeg ne mozhet otkryt' takoy inzhutor (exit 183) — propadaet
// vsa zapis' (job 17: stabil' 955 bayt ubil mikš 53-minutnoy vstrechi).
export const MIN_SEGMENT_BYTES = 1024;

export function filterUsableSegs(segs) {
  return (segs || []).filter((s) => (s && s.bytes) >= MIN_SEGMENT_BYTES);
}

// Proverka miksa polnym dekodom: amix s duration=longest dolzhen dl'sya ne korotche
// samogo dlinnogo dekodiryuemogo input (minus shkly na peremotku shvа). Yesli net —
// mikš umer na shkve i syedal khvost (job 16/17: 72 s iz 4000 s), luchshe vzyat'
// otdel'nyy fragment. mixDecodedS=0/NaN (dekod upal) — tozhe "khuzhe".
export function mixWorseThanInput(mixDecodedS, bestInputDecodedS, slackS = 60) {
  if (!Number.isFinite(bestInputDecodedS) || bestInputDecodedS <= 0) return false;
  if (!Number.isFinite(mixDecodedS) || mixDecodedS <= 0) return true;
  return bestInputDecodedS - mixDecodedS > slackS;
}

// Vybor luchshego varianta po POLNOMU DEKODU: cands [{p, decodedS, fixed?}].
// so skipped decodedS <= 0 (nie chiotsya). fixed (peresobrannyy chistyy
// konteyner) poluchaet bonus slackS — on tryaet neskol'ko frames na shkve
// perekodirovaniya, no probe/player rabotayut; syroye (bez bonusa) pobezhdayet
// tol'ko yesli FIXATNYY variant syedal by boleye slackS audio (mkvmerge,
// s"xvativshiy" 97% payload: 3980 s -> 72 s, — otvetdeny).
export function pickBestDecoded(cands, slackS = 60) {
  let best = null;
  for (const c of (cands || [])) {
    if (!c || !Number.isFinite(c.decodedS) || c.decodedS <= 0) continue;
    const score = c.decodedS + (c.fixed ? slackS : 0);
    if (best === null || score > best.score) best = { ...c, score };
  }
  return best;
}

// Offsety vseh EBML-zagolovkov v bufer. >1 = v odnom fayle skleeny
// neskolko MediaRecorder-potokov (shva bez shkly, dekor teryayet chtenie
// za pervym potokom — pattern job 16). Tajly takogo fayla rezhemsya po
// offsetam, kazhdaya chast' — samostoyatel'nyy inzhutor miksa.
export function ebmlHeaderOffsets(buf) {
  const offs = [];
  let i = 0;
  for (;;) {
    i = buf.indexOf(EBML_MAGIC, i);
    if (i < 0) break;
    offs.push(i);
    i += EBML_MAGIC.length;
  }
  return offs;
}
