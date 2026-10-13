// ffmpeg-helpers remontu zapisi (finalize v recorder.js). Otdel'no ot recorder.js,
// chtoby testy (node --test) ne tiahnuli playwright.
//
// Glavnaya problema job 16/17: obryvannyy MediaRecorder-bez-cues konteyner imeyet
// duration=N/A, probe=0. Amix bez ignore_err umerlaet na shkve, a mkvmerge
// «remontiruya» takoy, syedayet payload (34 MB -> 973 KB). Lechenie: vsegda
// validirovat' POLNYM DEKODOM i peresobirat' ffmpeg-reenkodom s ignore_err.
import fs from "node:fs";
import { execFileSync } from "node:child_process";

// Dlitel'nost' PO POLNOMU DEKODU (-progress pipe:1 / out_time_ms), ne po
// container-metadannym, kotorykh u obryva net. 0 yesli fayl sovschem nie chiotsya.
export function decodedAudioSeconds(p, timeoutMs = 600000) {
  try {
    const out = execFileSync("ffmpeg", ["-nostdin", "-nostats", "-v", "error",
      "-err_detect", "ignore_err", "-progress", "pipe:1", "-i", p, "-f", "null", "-"],
    { encoding: "utf8", timeout: timeoutMs, maxBuffer: 8 * 1024 * 1024 });
    let last = 0;
    for (const line of out.split(/[\r\n]+/)) {
      const m = /^out_time_ms=(\d+)/.exec(line.trim());
      if (m) last = Math.max(last, parseInt(m[1], 10) / 1e6);
    }
    return last;
  } catch { return 0; }
}

// Perekod v purezhenyy webm: chitaet skvoz' povrezhdeniya (ignore_err) i pishet
// konteyner s polnym indeksom — duration/probe posle etogo rabotayut.
export function reencodeWebm(src, dst, timeoutMs = 600000) {
  try {
    execFileSync("ffmpeg", ["-y", "-v", "error", "-err_detect", "ignore_err",
      "-i", src, "-map", "0:a:0", "-c:a", "libopus", "-b:a", "64k",
      "-ar", "48000", "-ac", "1", dst], { stdio: "pipe", timeout: timeoutMs });
    return fs.existsSync(dst) && fs.statSync(dst).size > 0;
  } catch { return false; }
}
