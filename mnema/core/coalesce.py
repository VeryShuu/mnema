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


@dataclass
class DisplaySegment:
    """Sploshnaya reperika dlya rendera: t_start pervogo fragmenta, t_end poslednego."""
    speaker_id: str | None
    speaker_name: str | None
    t_start: float
    t_end: float
    text: str
    needs_review: bool

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
        else:
            out.append(DisplaySegment(
                speaker_id=seg.speaker_id,
                speaker_name=seg.speaker_name,
                t_start=seg.t_start,
                t_end=seg.t_end if seg.t_end is not None else seg.t_start,
                text=text,
                needs_review=bool(seg.needs_review),
            ))
        last_end = seg.t_end if seg.t_end is not None else seg.t_start
    return out
