"""Testy razvyornutogo protokola: topic_summaries, changes (differensial s
predydushchey vstrechey), klyuchi shemy, merge v map-reduce, retender md/docx.

Shema obratno sovmestima: staryy JSON bez novyh klyuchey prodolzhaet prohodit
validate_protocol (klyuchi dobavlyayutsya so znacheniyami po umolchaniyu)."""
import datetime as dt
import json
import os

import pytest
from sqlalchemy import StaticPool, create_engine

os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive-detail")
os.environ.setdefault("DATA_DIR", "/tmp/test-data-detail")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-detail.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")


def _protocol_json(**overrides):
    d = {"topic": "T", "date": "", "duration": 0, "attendees": [],
         "agenda": [], "decisions": [], "action_items": [],
         "open_questions": []}
    d.update(overrides)
    return json.dumps(d, ensure_ascii=False)


@pytest.fixture()
def replies(monkeypatch):
    """Lovit soobshcheniya; otvet — JSON protokola (mozhno parametrizovat')."""

    class _Calls(list):
        state = None

    calls = _Calls()
    state = {"content": None}
    calls.state = state

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["messages"])
        content = state["content"] or _protocol_json()

        class R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": content}}]}
        return R()

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr("httpx.get",
                        lambda *a, **k: type("R", (), {"status_code": 404})())
    return calls


def _engine():
    from mnema.core.db import Base, set_engine_for_test
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    return engine


# ---- obratnaya sovmestimost shemy ------------------------------------------
def test_validate_fills_new_keys_defaults():
    from mnema.core.llm_client import validate_protocol
    data = validate_protocol(json.loads(_protocol_json()))
    assert data["topic_summaries"] == []
    assert data["changes"] == {"relative_to": "", "continuing": [],
                               "progress": [], "new": [], "unconfirmed": []}


def test_validate_keeps_new_keys():
    from mnema.core.llm_client import validate_protocol
    full = json.loads(_protocol_json(
        topic_summaries=[{"topic": "Zalog", "summary": "720 tys.",
                          "source_ts": "00:19:35-00:20:09"}],
        changes={"relative_to": "19.10.2026", "continuing": ["Контракт"],
                 "progress": ["Мост"], "new": ["Отчёт"], "unconfirmed": []}))
    data = validate_protocol(full)
    assert data["topic_summaries"][0]["topic"] == "Zalog"
    assert data["changes"]["relative_to"] == "19.10.2026"
    assert data["changes"]["progress"] == ["Мост"]


def test_validate_garbage_changes_coerced():
    from mnema.core.llm_client import validate_protocol
    data = validate_protocol(json.loads(_protocol_json(changes="net shemy")))
    assert data["changes"]["continuing"] == []
    assert data["changes"]["relative_to"] == ""


# ---- prompt -----------------------------------------------------------------
def test_prompt_requires_summaries_and_changes_markers(replies):
    from mnema.core.llm_client import SYSTEM_PROMPT
    assert "topic_summaries" in SYSTEM_PROMPT
    assert "ne podtverzhdeno" in SYSTEM_PROMPT
    assert "PREDYDUSHCHEGO SOVESHCHEANIYA" in SYSTEM_PROMPT
    assert "ODNO poruchenie = ODNA stroka" in SYSTEM_PROMPT


def test_prev_digest_in_user_prompt(replies):
    from mnema.core.llm_client import structure_transcript
    structure_transcript("0:00 Ivanov: privet", topic="Plan",
                         prev_digest="RESHENO: zapolnit zalog")
    user = replies[0][1]["content"]
    assert "PROTOKOL PREDYDUSHCHEGO SOVESHCHEANIYA" in user
    assert "RESHENO: zapolnit zalog" in user


def test_no_prev_digest_prompt_unchanged(replies):
    from mnema.core.llm_client import structure_transcript
    structure_transcript("0:00 Ivanov: privet", topic="Plan")
    user = replies[0][1]["content"]
    assert "PROTOKOL PREDYDUSHCHEGO" not in user


def test_prev_digest_in_map_reduce_windows(replies, monkeypatch):
    monkeypatch.setenv("LLM_WINDOW_CHARS", "100")
    from mnema.core.llm_client import structure_transcript
    text = "\n".join(f"{i}:00 Ivanov: slovo {i} " * 3 for i in range(10))
    structure_transcript(text, topic="T", prev_digest="RESHENO: predok")
    # v oknah diff ne nado (tol'ko v reduce) — no v poslednem (reduce) est'
    assert "RESHENO: predok" in replies[-1][1]["content"]


# ---- map-reduce merge --------------------------------------------------------
def test_map_reduce_merges_topic_summaries(replies, monkeypatch):
    monkeypatch.setenv("LLM_WINDOW_CHARS", "100")
    from mnema.core.llm_client import structure_transcript
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            content = _protocol_json(
                topic_summaries=[{"topic": "A", "summary": "odna",
                                  "source_ts": "00:00:01-00:00:09"}])
        elif calls["n"] == 2:
            content = _protocol_json(
                topic_summaries=[{"topic": "B", "summary": "dvtoraya",
                                  "source_ts": "00:00:10-00:00:19"}])
        else:
            content = replies.state["content"] or _protocol_json(
                topic_summaries=[{"topic": "A", "summary": "odna"}])

        class R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": content}}]}
        return R()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    text = "\n".join(f"{i}:00 Ivanov: slovo {i} " * 3 for i in range(10))
    data = structure_transcript(text, topic="T")
    assert data["topic_summaries"]  # reduce dal svodki
    assert calls["n"] >= 3


# ---- retender md ------------------------------------------------------------
class _Job:
    topic = "Планерка"
    diar_turns = None


def _data():
    return {
        "topic": "Планерка", "date": "26.10.2026", "duration": 2160,
        "attendees": [{"name": "Ivanov I.I."}], "agenda": ["Zalogi"],
        "topic_summaries": [
            {"topic": "Залоги", "summary": "Залог 720 тыс., возвращена половина.",
             "source_ts": "00:19:35-00:20:09"}],
        "decisions": ["Запретить обеспечительные платежи."],
        "action_items": [{"task": "Претензия", "owner": "Petrov",
                          "due": "27.10.2026", "source_ts": "00:24:38"}],
        "open_questions": ["Kuda denegsya?"],
        "changes": {"relative_to": "19.10.2026", "continuing": ["Залоги"],
                    "progress": ["Мост открыт"], "new": ["Отчёт по авансам"],
                    "unconfirmed": ["Рассрочка НДС"]},
    }


def test_render_markdown_sections():
    from mnema.core.protocol_md import render_markdown
    md = render_markdown(_Job(), _data(), [])
    assert "## Сводка по темам" in md
    assert "**Залоги.**" in md and "720 тыс." in md
    assert "[00:19:35-00:20:09]" in md
    assert "## Принятые решения" in md
    assert "## Поручения" in md and "| Основание |" in md
    assert "## Требует решения / уточнения и проверки" in md
    assert "## Изменения относительно предыдущей встречи от 19.10.2026" in md
    assert "**Продвижение.**" in md and "Мост открыт" in md
    assert "**Не подтверждено.**" in md and "Рассрочка НДС" in md


def test_render_markdown_duration_humane():
    from mnema.core.protocol_md import render_markdown
    md = render_markdown(_Job(), _data(), [])
    assert "**Длительность:** 36 мин" in md


def test_render_markdown_no_changes_block_when_empty():
    from mnema.core.protocol_md import render_markdown
    d = _data()
    d["changes"] = {"relative_to": "", "continuing": [], "progress": [],
                    "new": [], "unconfirmed": []}
    md = render_markdown(_Job(), d, [])
    assert "Изменения относительно" not in md


def test_render_markdown_old_json_compat():
    """Staryy json_data bez novyh klyuchey — rendersya bez padyeniya."""
    from mnema.core.protocol_md import render_markdown
    old = {"topic": "Старый", "date": "01.01.2026", "duration": 59,
           "attendees": [], "agenda": [], "decisions": ["r"],
           "action_items": [], "open_questions": []}
    md = render_markdown(_Job(), old, [])
    assert "**Длительность:** 59 с" in md
    assert "## Сводка по темам" not in md


def test_write_docx_new_sections(tmp_path):
    pytest.importorskip("docx")
    from docx import Document
    from mnema.core.protocol_md import write_docx
    out = tmp_path / "p.docx"
    write_docx(_Job(), _data(), [], out)
    text = "\n".join(p.text for p in Document(str(out)).paragraphs)
    assert "Сводка по темам" in text
    assert "Залоги." in text
    assert "Поручения" in text
    assert "Изменения относительно предыдущей встречи от 19.10.2026" in text


# ---- worker: digest predka ----------------------------------------------------
def test_prev_digest_takes_latest_earlier_same_topic():
    from sqlalchemy.orm import Session
    from mnema.core.db import get_session
    from mnema.core.models import Job, Protocol
    from mnema.worker.tasks import _prev_protocol_digest
    engine = _engine()
    with Session(engine) as s:
        older = Job(topic="Планерка", status="done",
                    ended_ts=dt.datetime(2026, 10, 19, 10, 0))
        other = Job(topic="Другая", status="done",
                    ended_ts=dt.datetime(2026, 10, 24, 10, 0))
        cur = Job(topic="Планерка", status="processing",
                  ended_ts=dt.datetime(2026, 10, 26, 10, 0))
        s.add_all([older, other, cur])
        s.commit()
        older_id, other_id, cur_id = older.id, other.id, cur.id
        s.add(Protocol(job_id=older_id, json_data={
            "decisions": ["Платить зарплату вовремя"],
            "action_items": [{"task": "Справка", "owner": "Petrov",
                              "due": "20.10.2026"}],
            "open_questions": ["Бюджет?"]}))
        s.add(Protocol(job_id=other_id, json_data={
            "decisions": ["ne nado v digest"]}))
        s.commit()
    with get_session() as s:
        digest, prev_date = _prev_protocol_digest(s, cur_id, "Планерка")
    assert "Платить зарплату вовремя" in digest
    assert "Справка" in digest
    assert "Бюджет?" in digest
    assert "ne nado v digest" not in digest
    assert prev_date == "19.10.2026"


def test_prev_digest_empty_without_topic_and_predok():
    from sqlalchemy.orm import Session
    from mnema.core.db import get_session
    from mnema.core.models import Job
    from mnema.worker.tasks import _prev_protocol_digest
    engine = _engine()
    with Session(engine) as s:
        cur = Job(topic="Новая тема", status="processing",
                  ended_ts=dt.datetime(2026, 10, 26, 10, 0))
        s.add(cur)
        s.commit()
        cur_id = cur.id
    with get_session() as s:
        assert _prev_protocol_digest(s, cur_id, "Новая тема") == ("", "")
        assert _prev_protocol_digest(s, cur_id, "") == ("", "")


# ---- duration po segmentam ----------------------------------------------------
def test_real_duration_uses_last_segment_end():
    from mnema.worker.tasks import _real_duration

    class S:
        def __init__(self, te):
            self.t_end = te
    assert _real_duration([S(10.4), S(2160.2), S(100)]) == 2160.0


def test_real_duration_empty_zero():
    from mnema.worker.tasks import _real_duration
    assert _real_duration([]) == 0.0
