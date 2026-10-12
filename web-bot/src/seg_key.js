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
