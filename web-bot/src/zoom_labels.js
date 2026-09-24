// Chiistyye predikaty i regex-stroki metok knopok Zoom vebl-klienta.
// Strokovyye istochniki peredayutsya v page.evaluate, predikaty — dlya testov
// (node --test web-bot/test/).
//
// Vazhno: v personal'noy komnate (PMN, /my/...) krasnaya knopka zvetsya
// "End" / "End meeting", a NE "Leave" (vstrecha 39: "knopka Leave nie naydena").

const norm = (t) => String(t || "").trim().toLowerCase();
const re = (src) => new RegExp(src, "i");

// --- knopka mikrofona -------------------------------------------------------
export const MIC_CANDIDATE_SRC = "microphone|unmute|\\bmic\\b|микрофон|audio";
// Nadpisi, oznachayushchiy "klik — vklyuchim", t.ye. mikrofon UZHE vyklyuchen.
export const MIC_MUTED_SRC = "unmute|start audio|включить микрофон|подключить микрофон";
// Nadpisi aktivnogo mikrofona: klik po nikh vyklyuchaet.
export const MIC_ACTIVE_SRC = "\\bmute\\b|stop audio|выключить микрофон|отключить микрофон";
// Ikonka s perekhyorstiem/outline = vyknuchennyy (microphone-muted i t.p.).
export const MIC_MUTED_ICON_SRC = "muted|slash";

export function isMicCandidate(label) {
  return re(MIC_CANDIDATE_SRC).test(norm(label));
}

// true = vyklyuchen, false = vklyuchen, null = sostoyaniye nie opredelyaetsya
// po odnoy tol'ko nadpisi (chto-by nie klikat' vslepuyu).
export function micMutedFromLabel(label) {
  const t = norm(label);
  const muted = re(MIC_MUTED_SRC).test(t);
  const active = re(MIC_ACTIVE_SRC).test(t);
  if (muted && !active) return true;
  if (active && !muted) return false;
  return null;
}

// --- knopka vyhoda iz sozwona (Leave v obychnoy vstreche, End v PMN) --------
export const LEAVE_SRCS = [
  "leave meeting", "leave this meeting", "выйти (с|из) (встречи|созвона)",
  "оставить встречу", "покинуть встречу",
  "end meeting", "end for all", "end all meetings", "end call", "^end$",
  "завершить (встреч|созвон)", "закончить (встреч|созвон)",
];

// Dialog podtverzhdeniya posle Leave/End.
export const LEAVE_DIALOG_SRCS = [
  "leave meeting", "leave this meeting", "выйти (с|из) (встречи|созвона)",
  "disconnect", "отцепиться",
  "end meeting", "end for all", "end all meetings", "end your meeting",
  "завершить (встреч|созвон)", "закончить (встреч|созвон)",
];

// Odin rezhim dlya klassifikatora stranitsy: priznak "ya v zale".
export const END_CONTROL_SRC = LEAVE_SRCS.join("|");

function matchesAny(label, srcs) {
  const t = norm(label);
  if (!t) return false;
  return srcs.some((s) => re(s).test(t));
}

export function isLeaveMeetingLabel(label) {
  return matchesAny(label, [
    "leave meeting", "leave this meeting", "выйти (с|из) (встречи|созвона)",
    "оставить встречу", "покинуть встречу", "^leave$",
  ]);
}

// "End", "End meeting (for all)", "End all meetings…" — NO "recommend" "send":
// varianty s golym "end" trebuyut granitsy slova.
export function isEndMeetingLabel(label) {
  return matchesAny(label, [
    "^end$", "^end\\s", "end meeting", "end for all", "end all meetings",
    "end your meeting", "end call",
    "завершить (встреч|созвон)", "закончить (встреч|созвон)",
  ]);
}

export function isJoinedControlLabel(label) {
  return isLeaveMeetingLabel(label) || isEndMeetingLabel(label);
}
