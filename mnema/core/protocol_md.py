"""Sborka protokola (FT-18/FT-19): Markdown + DOCX.

DOCX po shablonu templates/protocol.docx yesli est'; inache baziimy dokument.
"""
from __future__ import annotations

from pathlib import Path

from .coalesce import coalesce_segments, speaker_display


def _fmt(sec: float) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def render_markdown(job, data: dict, segments: list) -> str:
    segments = coalesce_segments(segments)
    lines: list[str] = []
    topic = (data.get("topic") or job.topic)
    lines.append(f"# Протокол: {topic}")
    lines.append("")
    lines.append(f"- **Дата:** {data.get('date', '')}")
    if data.get("duration"):
        lines.append(f"- **Длительность:** {int(data['duration'])} с")
    att = data.get("attendees") or []
    if att:
        lines.append("- **Говорящие (по голосу):** " + ", ".join(
            a.get("name", "?") for a in att))
    lines.append("")
    if data.get("agenda"):
        lines.append("## Повестка")
        for a in data["agenda"]:
            lines.append(f"- {a}")
        lines.append("")
    if data.get("decisions"):
        lines.append("## Решения")
        for d in data["decisions"]:
            lines.append(f"- {d}")
        lines.append("")
    items = data.get("action_items") or []
    if items:
        lines.append("## Задачи")
        lines.append("| Задача | Ответственный | Срок | Источник |")
        lines.append("|---|---|---|---|")
        for ai in items:
            lines.append(f"| {ai.get('task', '')} | {ai.get('owner') or '—'} | "
                         f"{ai.get('due') or '—'} | {ai.get('source_ts') or ''} |")
        lines.append("")
    if data.get("open_questions"):
        lines.append("## Открытые вопросы")
        for q in data["open_questions"]:
            lines.append(f"- {q}")
        lines.append("")
    lines.append("## Стенограмма")
    lines.append("")
    for seg in segments:
        name = speaker_display(seg)
        flag = " [треб.уточнения]" if seg.needs_review else ""
        lines.append(f"`{_fmt(seg.t_start)}` **{name}**{flag}: {seg.text}")
    lines.append("")
    return "\n".join(lines)


def write_docx(job, data: dict, segments: list, path: Path) -> None:
    from docx import Document
    segments = coalesce_segments(segments)
    template = Path(__file__).resolve().parents[2] / "templates" / "protocol.docx"
    doc = Document(str(template)) if template.exists() else Document()
    topic = data.get("topic") or job.topic
    doc.add_heading(f"Протокол: {topic}", level=0)
    doc.add_paragraph(f"Дата: {data.get('date', '')}")
    if data.get("agenda"):
        doc.add_heading("Повестка", level=1)
        for a in data["agenda"]:
            doc.add_paragraph(a, style="List Bullet")
    if data.get("decisions"):
        doc.add_heading("Решения", level=1)
        for d in data["decisions"]:
            doc.add_paragraph(d, style="List Bullet")
    items = data.get("action_items") or []
    if items:
        doc.add_heading("Задачи", level=1)
        table = doc.add_table(rows=1, cols=4)
        hdr = table.rows[0].cells
        hdr[0].text, hdr[1].text, hdr[2].text, hdr[3].text = "Задача", "Ответственный", "Срок", "Источник"
        for ai in items:
            row = table.add_row().cells
            row[0].text = ai.get("task", "")
            row[1].text = ai.get("owner") or "—"
            row[2].text = ai.get("due") or "—"
            row[3].text = ai.get("source_ts") or ""
    doc.add_heading("Стенограмма", level=1)
    for seg in segments:
        doc.add_paragraph(f"{_fmt(seg.t_start)} {speaker_display(seg)}: {seg.text}")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


def assemble_protocol(job, data: dict, segments: list, out_dir: Path) -> tuple[str, str]:
    """Pishi md i docx v arhiv; vozvraschaet puti."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "protocol.md"
    md.write_text(render_markdown(job, data, segments), encoding="utf-8")
    docx = out_dir / "protocol.docx"
    try:
        write_docx(job, data, segments, docx)
        docx_path = str(docx)
    except Exception:  # noqa: BLE001 — docx nie kritichen (md glavnyy)
        docx_path = None
    return str(md), docx_path


def refresh_protocol(pid: int) -> bool:
    """Perepishi md/docx protokola po teperishnim dannym BDB (imena govoryashchih,
    source_ts). Fix: faily generirovalis' odnokratno v `_build_protocol` i ostavalis'
    s 'Golos SPEAKER_XX' posle utochneniya imen operatorom."""
    from sqlalchemy import select

    from .db import get_session
    from .models import Job, Protocol, Segment
    with get_session() as s:
        p = s.get(Protocol, pid)
        if p is None or not p.md_path:
            return False
        job = s.get(Job, p.job_id)
        segs = list(s.scalars(select(Segment).where(
            Segment.job_id == p.job_id, Segment.excluded.is_(False)
        ).order_by(Segment.t_start)).all())
        data = dict(p.json_data or {})
        md_path = Path(p.md_path)
        docx_path_str = p.docx_path
    if job is None:
        return False
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(job, data, segs), encoding="utf-8")
    if docx_path_str:
        try:
            write_docx(job, data, segs, Path(docx_path_str))
        except Exception:  # noqa: BLE001 — docx nie kritichen (md glavnyy)
            pass
    from . import export
    export.export_protocol_files(p.job_id)  # zerkalo: vnutri try/except
    return True
