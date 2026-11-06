"""Predvaritel'nyy probeg stenogrammy pered otobrazheniem/vygruzkoy (FT-usability).

Ustranyaet artefakty raspoznavaniya v vide dlya pol'zovatelya:
  1. Реплики без текста (VAD vydal segment, ASR ne rasponal -> text="").
  2. Povtoryayushchiiesya odinakovye golosos: VAD rechet rech' po 15 s i po
     pauzam, poetomu u odnogo govoryashchogo idut neskol'ko podryad stoek
     "Golos 2 / Golos 2 / ...". Skleivaem sosednie segmenty odnogo govoryashchego
     v odnu reperiku.

BDD NE trogaetsya: syryye segmenty nuzhny dlya revizii operatorom, otklyucheniya
segmentov i peremotki audio; chistka primenyaetsya tol'ko pri renderinge
(stenogramma, protokoll md/docx, stranitsa protokola, vhod dlya LLM).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MAX_GAP_S = 20.0
DEFAULT_MIN_PART_COVERAGE = 0.35


@dataclass
class DisplaySegment:
    """Sploshnaya reperika dlya rendera: t_start pervogo fragmenta, t_end poslednego."""
    speaker_id: str | None
    speaker_name: str | None
    t_start: float
    t_end: float
    text: str
    needs_review: bool
    two_voices: bool = False
    pending_review: bool = False

    @property
    def display_name(self) -> str:
        return speaker_display(self)


def speaker_display(seg) -> str:
    """Otobrazhaemoe imya govoryashchego: imya iz profilya ili fallsbek "Golos N"."""
    return seg.speaker_name or f"Голос {seg.speaker_id or '?'}"


def _speaker_key(seg) -> tuple:
    """Klyuch govoryashchogo dlya gruppirovki: imya vazhne klastera (posle
    utochneniya imeni raznye klastery mogut stat' odnim chelovekom); bez imeni —
    klaster diarizacii."""
    name = (seg.speaker_name or "").strip().lower()
    return ("name", name) if name else ("cluster", seg.speaker_id)


def max_gap_s() -> float:
    """Maksimal'nyy zazor mezhdu fragmentami dlja sliyanija (s)."""
    raw = os.environ.get("COALESCE_MAX_GAP_S", "").strip()
    try:
        return float(raw) if raw else DEFAULT_MAX_GAP_S
    except ValueError:
        return DEFAULT_MAX_GAP_S


def split_turns_enabled() -> bool:
    """Разбивать VAD-строку на реплики по границам говорящих (diar turns)."""
    return os.environ.get("COALESCE_SPLIT_TURNS", "1").strip() != "0"


def min_part_coverage() -> float:
    """Доля покрытия чужим голосом ASR-части, при которой она отделяется."""
    raw = os.environ.get("COALESCE_PART_MIN_COVERAGE", "").strip()
    try:
        return float(raw) if raw else DEFAULT_MIN_PART_COVERAGE
    except ValueError:
        return DEFAULT_MIN_PART_COVERAGE


def _part_label(turns: list, s: float, e: float, fallback: str | None,
                min_cov: float) -> str | None:
    """Голос ASR-части: доминантный, если он покрывает часть не хуже min_cov;
    иначе — метка строки (мелкое наложение чужого turn-а не режем)."""
    from .diarize import _dominant_label
    label, ratio = _dominant_label(turns, s, e)
    if label and ratio >= min_cov:
        return label
    return fallback


def _two_voices(seg) -> bool:
    """Фрагмент с наложением двух голосов (multi_voice) либо часть такой VAD-строки."""
    return bool(getattr(seg, "two_voices", False)) or \
        getattr(seg, "speaker_source", None) == "multi_voice"


def _pending_review(seg) -> bool:
    """Нужно уточнение НЕ из-за наложения голосов: multi_voice needs_review
    ложится на весь фрагмент и уточняется не оператором, а диаризацией."""
    return bool(seg.needs_review) and not _two_voices(seg)


def expand_segments(segments: list, turns: list | None = None) -> list:
    """Резать VAD-строки, в которых два голоса, на отдельные реплики.

    Механизм ошибки: VAD режет речь по паузам, а не по говорящим, поэтому
    смена голоса без паузы попадает внутрь одного VAD-сегмента, и вся строка
    получала метку доминантного голоса (фразы второго человека писались от
    первого). Здесь строка разбивается по своим ASR-частям (segments.asr_json
    — точные timeshampы фраз), голос каждой части определяется по turns
    диаризации, имя берётся из сегментов-резидентов того же кластера.

    Без turns / без asr_json / при одном голосе на всех частях — без изменений."""
    if not turns or not split_turns_enabled():
        return [s for s in segments if (s.text or "").strip()]
    min_cov = min_part_coverage()
    names: dict[str, str | None] = {}
    for seg in segments:
        lbl = getattr(seg, "diar_label", None) or seg.speaker_id
        if lbl and seg.speaker_name and not names.get(lbl):
            names[lbl] = seg.speaker_name
    out: list = []
    for seg in segments:
        if not (seg.text or "").strip():
            continue
        parts = [p for p in (getattr(seg, "asr_json", None) or [])
                 if (p.get("t") or "").strip()]
        if len(parts) < 2:
            out.append(seg)
            continue
        base = getattr(seg, "diar_label", None) or seg.speaker_id
        labels = [_part_label(turns, float(p.get("s", 0)), float(p.get("e", 0)),
                              base, min_cov) for p in parts]
        if len(set(labels)) <= 1:
            out.append(seg)
            continue
        grouped: list[tuple[str | None, list[dict]]] = []
        cur_lbl, cur = None, []
        for p, lbl in zip(parts, labels):
            if cur_lbl is None:
                cur_lbl = lbl
            if lbl == cur_lbl:
                cur.append(p)
            else:
                grouped.append((cur_lbl, cur))
                cur_lbl, cur = lbl, [p]
        if cur:
            grouped.append((cur_lbl, cur))
        for lbl, grp in grouped:
            out.append(_piece(seg, grp, lbl, base, names))
    return out


def _piece(seg, parts: list[dict], label: str | None, base: str | None,
           names: dict[str, str | None]):
    """Одна реплика из части VAD-строки: своё время, текст, говорящий."""
    from types import SimpleNamespace
    text = " ".join((p.get("t") or "").strip() for p in parts).strip()
    is_base = (label == base)
    name = names.get(label) or (seg.speaker_name if is_base else None)
    return SimpleNamespace(
        speaker_id=label or seg.speaker_id,
        speaker_name=name,
        diar_label=label,
        speaker_source=(getattr(seg, "speaker_source", None) or "unresolved")
        if is_base else ("voiceprint" if name else "unresolved"),
        t_start=float(parts[0].get("s", seg.t_start)),
        t_end=float(parts[-1].get("e", seg.t_end)),
        text=text,
        needs_review=bool(seg.needs_review) if is_base else not bool(name),
        two_voices=_two_voices(seg) if is_base else False,
    )


def coalesce_segments(segments: list) -> list[DisplaySegment]:
    """Ubiraet pustye reperiki i skleivaet sosednie segmenty odnogo govoryashchego.

    Segmenty dolzhny byt' otsortirovany po t_start (kak vo vsekh vyklydayushchih
    zachikah). Poryadok i vremya pervoy reperiki sohranyayutsya; pri sliyanii
    t_end bereutsya iz poslednego fragmenta, needs_review — YESLI khotya by u
    odnogo. Sliyanie tol'ko pri zazore ne bolee max_gap_s: replitu odnogo
    govoryashchogo spustya bol'shuyu pauzu ostavlyaem otdel'noy reperikoy.
    """
    gap_limit = max_gap_s()
    out: list[DisplaySegment] = []
    last_end: float | None = None  # t_end predydushchego syrogo segmenta (s textom)
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        prev = out[-1] if out else None
        gap = (seg.t_start - last_end) if last_end is not None else 0.0
        if (prev is not None and _speaker_key(prev) == _speaker_key(seg)
                and gap <= gap_limit):
            prev.text = f"{prev.text} {text}"
            prev.t_end = max(prev.t_end, seg.t_end or 0.0)
            prev.needs_review = prev.needs_review or bool(seg.needs_review)
            prev.two_voices = prev.two_voices or _two_voices(seg)
            prev.pending_review = prev.pending_review or _pending_review(seg)
        else:
            out.append(DisplaySegment(
                speaker_id=seg.speaker_id,
                speaker_name=seg.speaker_name,
                t_start=seg.t_start,
                t_end=seg.t_end if seg.t_end is not None else seg.t_start,
                text=text,
                needs_review=bool(seg.needs_review),
                two_voices=_two_voices(seg),
                pending_review=_pending_review(seg),
            ))
        last_end = seg.t_end if seg.t_end is not None else seg.t_start
    return out
