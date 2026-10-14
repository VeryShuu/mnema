// Finalize zapis' i vosstanovlenie po syrym segmentam. Vyezhanо iz recorder.js,
// chtoby testy (node --test) i salvage put' ne tyanuli playwright.
//
// Finalize: syryye segmenty (raw.<n>_w<W>.webm, po odnomu na freym-payplayn/okno)
// skleyayutsya adelay+amix v odin webm s validaciey PO POLNOMU DEKODU; pri
// podozrenii na poteru syryye fajly SOHRANYAYUTSIA dlya ruchnogo vosstanovleniya.
//
// salvageFromDisk: zagreshennaya web-bot (restart/ubijstvo) ostavlyaet na diske
// raw.*_w*.webm bez final'nogo webm — sobiraem ix po imenam (poryadok n =
// poryadok sozdaniya) s otsenkoy epoh po bajtam i pushaem v tot ze finalize.
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { shortMixWarningS, filterUsableSegs, ebmlHeaderOffsets, mixWorseThanInput, pickBestDecoded, MIN_SEGMENT_BYTES } from "./seg_key.js";
import { decodedAudioSeconds, reencodeWebm } from "./webm_fix.js";
import { parseRawSegName } from "./record_guard.js";

const ERROR = "error";

// ------------------------------------------------------------- finalize FT-6/41
// segs: [{p, bytes, epochMs}] — po odnomu webm-potoku na freym-payplayn;
// obshchiy khronometrazh — epochMs (Date.now momenta rec.start() po chasu
// konteynera). Baza shkly — pervyy segment. Odin segment bez shvona — prosto
// kopiruem. Vozvrat: {webm, wav|null, mp3|null, duration_s, sizes}; ok opredelyaet
// vyzyvayushchiy po nalichiyu/readable webm.
export function finalize(jobId, segs, webmPath, wavPath, mp3Path, state, end, onEvent) {
  let durationS = null;
  const tooSmall = (segs || []).filter((s) => s.bytes > 0 && s.bytes < MIN_SEGMENT_BYTES);
  const real = filterUsableSegs((segs || []).filter((s) => s.bytes > 0 && fs.existsSync(s.p)
    && fs.statSync(s.p).size > 0))
    .sort((a, b) => (a.epochMs || 0) - (b.epochMs || 0));
  if (tooSmall.length)
    onEvent(ERROR, `отброшено сегментов <1 KiB: ${tooSmall.length} — amix падает на нечитаемом входе целиком (job 17)`);
  // 1) Strahovka odnogo iznosa: esli v segment zapisan >1 EBML-zagolovka, vnutri
  // nego skleeny neskolko MediaRecorder-potokov (shva) — dekor teryayet chtenie
  // za pervym. Rezhem po offsetam; chasti idut v miksh otdel'no (epoch sleduyushchey
  // chasti otsenivaemsya po bajtam: ~8 KB/s pri 64 kbps).
  const inputs = [];
  for (const s of real) {
    let parts = [s];
    try {
      const offs = ebmlHeaderOffsets(fs.readFileSync(s.p));
      if (offs.length > 1) {
        onEvent(ERROR, `segment ${path.basename(s.p)}: skleeny ${offs.length} potokov — rezu po shvam`);
        parts = [];
        for (let k = 0; k < offs.length; k++) {
          const from = offs[k];
          const to = k + 1 < offs.length ? offs[k + 1] : fs.statSync(s.p).size;
          if (to - from < 1024) continue; // sluchaynoe sovpadenie magic v opus-dannyh
          const pp = `${s.p}.p${k}.webm`;
          const wfd = fs.openSync(pp, "w");
          const chunk = Buffer.allocUnsafe(to - from);
          const rfd = fs.openSync(s.p, "r");
          fs.readSync(rfd, chunk, 0, to - from, from);
          fs.writeSync(wfd, chunk);
          fs.closeSync(rfd); fs.closeSync(wfd);
          const epochMs = (s.epochMs || 0)
            + Math.round((k === 0 ? 0 : offs[k] - offs[0]) / 8); // bajt -> ms pri 64kbps
          parts.push({ p: pp, bytes: to - from, epochMs, partOf: s.p });
        }
      }
    } catch { /* ne procitalis' — kak est' odnim fragmentom */ }
    inputs.push(...parts);
  }
  const base = (inputs[0] && inputs[0].epochMs) || Date.now();
  const tmpMix = webmPath + ".mix.webm";
  try { fs.rmSync(tmpMix, { force: true }); } catch { /* */ }
  try {
    if (inputs.length === 1) {
      fs.copyFileSync(inputs[0].p, tmpMix);
    } else {
      try {
        mixSegments(inputs, base, tmpMix);
      } catch (e2) {
        onEvent(ERROR, `микс упал (${String(e2.message).slice(0, 120)}) — проверяю фрагменты`);
      }
    }
    // Validaciya rezul'tata POLNYM DEKODOM: amix mozhet upast' na povrezhdennoy
    // vkhode uzhe na shkve, ostaviv nepustoy obryv, a probe takogo i vovse ne
    // pokazyvaet — dal'niy «remont» mkvmerge s"edal dannye (job 17: 3980 s ->
    // 72 s). Esli mikš korotche luchshego dekodiruyemogo fragmenta, sozdayem
    // vyvod peresborkoy-totogo-fragmenta reenkodom (chitaet cherez povrezhdeniya).
    let useFallback = false, bestSeg = null;
    const mixDec = fs.existsSync(tmpMix) ? decodedAudioSeconds(tmpMix) : 0;
    if (inputs.length > 1) {
      let bestDec = 0;
      for (const s of inputs) {
        const d = decodedAudioSeconds(s.p);
        if (d > bestDec) { bestDec = d; bestSeg = s; }
      }
      useFallback = bestSeg !== null && mixWorseThanInput(mixDec, bestDec);
      if (useFallback)
        onEvent(ERROR, `микс прочитался на ${Math.round(mixDec)} c из ${Math.round(bestDec)} c `
          + `— восстанавливаю реэнкодом фрагмента ${path.basename(bestSeg.p)}`);
    }
    if (useFallback && reencodeWebm(bestSeg.p, webmPath)) { /* gotovo */ }
    else if (!useFallback && mixDec > 0) fs.renameSync(tmpMix, webmPath);
    else if (fs.existsSync(tmpMix) && fs.statSync(tmpMix).size > 0) fs.renameSync(tmpMix, webmPath);
    else {
      const biggest = [...inputs].sort((a, b) => b.bytes - a.bytes)[0];
      if (biggest) { try { fs.copyFileSync(biggest.p, webmPath); } catch { /* */ } }
    }
  } catch (e) {
    /* bolshe vsego potokov — beriem naibolee polnyy bez shkly */
    if (!fs.existsSync(webmPath) || fs.statSync(webmPath).size === 0) {
      const biggest = [...inputs].sort((a, b) => b.bytes - a.bytes)[0];
      if (biggest) { try { fs.copyFileSync(biggest.p, webmPath); } catch { /* */ } }
    }
  } finally {
    try { fs.rmSync(tmpMix, { force: true }); } catch { /* */ }
  }

  // pocinka povrezhdёnogo webm (FT-6), yesli konteyner ne chitaetsya. Berem
  // variant s nejdl'shim POLNOMU DEKODOM. Sravnivaem varianty PO POLNOMU DEKODU
  // (ffmpeg-reencode s ignore_err, mkvmerge, sam Syracuse) i berem nejdl'shiy:
  // mkvmerge na obryvanom bez cues mozhet s"est' 97% payload (job 17).
  if (!probeDuration(webmPath)) {
    const cands = [{ p: webmPath, decodedS: decodedAudioSeconds(webmPath) }];
    const ff = webmPath + ".fffix.webm";
    if (reencodeWebm(webmPath, ff)) cands.push({ p: ff, decodedS: decodedAudioSeconds(ff), fixed: true });
    try {
      const fixed = webmPath + ".fixed.webm";
      execFileSync("mkvmerge", ["-o", fixed, webmPath], { stdio: "pipe" });
      if (fs.existsSync(fixed) && fs.statSync(fixed).size > 0)
        cands.push({ p: fixed, decodedS: decodedAudioSeconds(fixed), fixed: true });
    } catch (e) { /* mkvmerge nie vsetda spasayet */ }
    const winner = pickBestDecoded(cands);
    for (const c of cands)
      if (winner && c.p !== winner.p && c.p !== webmPath) { try { fs.rmSync(c.p, { force: true }); } catch { /* */ } }
    if (winner && winner.p !== webmPath) { try { fs.renameSync(winner.p, webmPath); } catch { /* */ } }
  }
  try {
    durationS = probeDuration(webmPath);
  } catch { durationS = null; }
  // sanity: микс заметно короче wall-clock записи — потерянный хвост (job
  // 16) — loudим в events operatora I SOHRANYAEM syryye segmenty: bez nih
  // vstrecha nevosstanovimaya.
  let suspect = false;
  if (state.joinedAt) {
    const warnS = shortMixWarningS(durationS, (Date.now() - state.joinedAt) / 1000);
    if (warnS !== null) {
      suspect = true;
      onEvent(ERROR, `длительность записи ${Math.round(durationS)} c заметно короче встречи `
        + `(${Math.round((Date.now() - state.joinedAt) / 1000)} c) — часть аудио потеряна при склейке`);
    }
  }
  // 2) Sverka: browser (nakopitel'nyy __mnemaTotal po vsem dokumentam) vs zapisannoye
  // na disk. Bol'shaya nesootvetstvie = propadeny mezhdu drainami — tozhe sohranyaem.
  const writtenByDoc = new Map();
  for (const s of segs) {
    if (!s.key) continue;
    const docPart = s.key.split("|")[1] || "";
    writtenByDoc.set(docPart, (writtenByDoc.get(docPart) || 0) + s.bytes);
  }
  let browserBytes = 0, writtenBytes = 0;
  for (const [k, tot] of Object.entries(state.browserTotal || {})) {
    const docPart = k.split("|")[1] || "";
    browserBytes += tot;
    writtenBytes += writtenByDoc.get(docPart) || 0;
  }
  if (browserBytes > 1_000_000 && writtenBytes < browserBytes * 0.9) {
    suspect = true;
    onEvent(ERROR, `записано ${writtenBytes} байт из ${browserBytes}, выброшенных браузером — часть аудио потеряна`);
  }
  // wav i mp3 (mp3 srazu — FT-41a)
  let wavOk = false, mp3Ok = false;
  try {
    execFileSync("ffmpeg", ["-y", "-err_detect", "ignore_err", "-i", webmPath,
      "-ar", "16000", "-ac", "1", wavPath], { stdio: "pipe" });
    wavOk = fs.existsSync(wavPath);
  } catch (e) { /* worker povtorit */ }
  try {
    execFileSync("ffmpeg", ["-y", "-err_detect", "ignore_err", "-i", webmPath,
      "-ar", "44100", "-ac", "1", "-b:a", "64k", mp3Path], { stdio: "pipe" });
    mp3Ok = fs.existsSync(mp3Path);
  } catch (e) { /* mp3 encoder niedostupen — razresheno vydat webm */ }
  const sizes = {};
  if (suspect) {
    // ne lonim syryye fajly: pri podozrenii na poteru eto edinstvennaya
    // vozmozhnost' vosstanovit' vstrechu vruchnuyu (raw.*_w*.webm v kataologe job)
    onEvent(ERROR, "сырые сегменты сохранены в каталоге задачи для ручного восстановления");
  } else {
    for (const p of segs) { try { fs.rmSync(p.p, { force: true }); } catch { /* */ } }
  }
  for (const p of inputs) {
    if (!p.partOf) continue; // vydvizhki razrezki — vsegda korotkie: udalyaem
    try { fs.rmSync(p.p, { force: true }); } catch { /* */ }
  }
  for (const [k, p] of [["webm", webmPath], ["wav", wavPath], ["mp3", mp3Path]])
    if (fs.existsSync(p)) sizes[k] = fs.statSync(p).size;
  return {
    ok: true, webm: webmPath, wav: wavOk ? wavPath : null,
    mp3: mp3Ok ? mp3Path : null, duration_s: durationS, sizes,
    end_reason: (end && end.reason) || "salvage",
  };
}

// atrim do armEpoch + adelay posle nego + amix v odin webm (normalize=0 —
// bez padaniya levelya). Vse segmenty na odnoy globalnoy shkalе vremeni.
export function mixSegments(real, base, outPath) {
  // ignore_err: povrezhdennyye kadry v seredine potoka nie dolzhny umerbishch'
  // ves' amix — eto input-opciya, pered kazhdym -i (dlya vseh vvodov).
  const args = ["-y"];
  for (const s of real) args.push("-err_detect", "ignore_err", "-i", s.p);
  const parts = [];
  const labels = [];
  real.forEach((s, i) => {
    const epoch = s.epochMs ?? base;
    const delayMs = Math.max(0, Math.round(epoch - base));
    parts.push(`[${i}:a]aresample=48000,adelay=${delayMs}|${delayMs}[a${i}]`);
    labels.push(`[a${i}]`);
  });
  const chain = labels.length === 1
    ? `${labels[0]}anull[out]`
    : `${labels.join("")}amix=inputs=${real.length}:duration=longest:normalize=0[out]`;
  parts.push(chain);
  execFileSync("ffmpeg", [...args,
    "-filter_complex", parts.join(";"),
    "-map", "[out]", "-c:a", "libopus", "-b:a", "64k", outPath],
  { stdio: "pipe", timeout: 600000 });
}

export function probeDuration(p) {
  try {
    const out = execFileSync("ffprobe", ["-v", "quiet", "-print_format", "json",
      "-show_format", p], { encoding: "utf8" });
    return parseFloat(JSON.parse(out).format.duration) || 0;
  } catch { return 0; }
}

// ------------------------------------------- salvage: zapisi net, syryye fayly yest'
// Po kataloge sobraniya sobiraem syryye segmenty `raw.<n>_w<W>.webm` (n —
// poryadok sozdaniya, hronologicheskiy) v segmenty finalize. Epoha, kotoroyu
// pomnixat tol'ko v pamyati web-bot, na diske otsutstvuyet — otsenivaem
// napolneniye: epoch(k) = epoch(k-1) + bytes(k-1)/8 (ms pri 64 kbps, ~8 KB/s),
// kак i pri razrezke EBML-shvov v finalize.
export function rawSegsFromDisk(jobDir) {
  let names;
  try { names = fs.readdirSync(jobDir); } catch { return []; }
  const segs = [];
  for (const name of names) {
    const m = parseRawSegName(name);
    if (!m) continue; // ne syroy segment (splits `.p<k>.webm` i procheyi ne podkhodyat)
    const p = path.join(jobDir, name);
    let st;
    try { st = fs.statSync(p); } catch { continue; }
    if (!st.isFile()) continue;
    segs.push({ p, bytes: st.size, n: m.n, epochMs: 0 });
  }
  segs.sort((a, b) => a.n - b.n);
  let acc = 0;
  for (const s of segs) {
    s.epochMs = acc;
    acc += Math.round(s.bytes / 8);
  }
  return segs;
}

// Vosstanovlenie zadachi Po syrym segmentam na diske (web-bot umen/skonchilsya
// mid-zapisi). Vozvrat kaka v finalize, no ok=false yesli validnogo vyvoda net;
// nichyego ne brosaet isklyucheniyami.
export function salvageFromDisk(jobId, cfg, onEvent = () => {}) {
  const jobDir = path.join(cfg.dataDir, "jobs", String(jobId));
  const webmPath = path.join(jobDir, "raw.webm");
  const wavPath = path.join(jobDir, "meeting.wav");
  const mp3Path = path.join(jobDir, "meeting.mp3");
  if (fs.existsSync(webmPath) && fs.statSync(webmPath).size > 0) {
    // finalize uzhe probegal, no rezultat mog ne doyti do API (web-bot umirl
    // mezhdu finalize i POST /finished) — otdaj puti dlya povtornogo finished
    const sizes = {};
    for (const [k, p] of [["webm", webmPath], ["wav", wavPath], ["mp3", mp3Path]])
      if (fs.existsSync(p)) sizes[k] = fs.statSync(p).size;
    return {
      ok: true, already_finalized: true, webm: webmPath,
      wav: fs.existsSync(wavPath) ? wavPath : null,
      mp3: fs.existsSync(mp3Path) ? mp3Path : null,
      duration_s: probeDuration(webmPath) || null, sizes,
      end_reason: "already_finalized",
    };
  }
  const segs = rawSegsFromDisk(jobDir);
  if (!segs.some((s) => s.bytes >= MIN_SEGMENT_BYTES)) {
    return { ok: false, fail_reason: "no_raw_segments" };
  }
  const state = { joinedAt: null, browserTotal: {} };
  try {
    const res = finalize(jobId, segs, webmPath, wavPath, mp3Path,
      state, { reason: "salvaged" }, onEvent);
    const good = fs.existsSync(webmPath) && fs.statSync(webmPath).size > MIN_SEGMENT_BYTES;
    return { ...res, ok: good };
  } catch (e) {
    onEvent(ERROR, `salvage: ${String(e.message).slice(0, 160)}`);
    return { ok: false, fail_reason: "salvage_error", error: String(e.message) };
  }
}
