"""Testy peredachi dolzhnostey (roster) v LLM-prompt.

Spravka 'FIО — dolzhnost' dolzhna popast' v user-soobshchenie i v map-reduce
okna; bez rostera prompt ne menyayetsya. V protokole dolzhnosti ne dolzhny
otrazhat'sya (shema ne menyalsya).
"""
import json
import os

import pytest
from sqlalchemy import StaticPool, create_engine

os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive-roster")
os.environ.setdefault("DATA_DIR", "/tmp/test-data-roster")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-roster.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")

VALID = json.dumps({"topic": "T", "date": "", "duration": 0, "attendees": [],
                    "agenda": [], "decisions": [], "action_items": [],
                    "open_questions": []}, ensure_ascii=False)


@pytest.fixture()
def replies(monkeypatch):
    """Lovit soobshcheniya, otvechaet valid JSON-om protokola."""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["messages"])
        content = VALID

        class R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": content}}]}
        return R()

    monkeypatch.setattr("httpx.post", fake_post)
    return calls


def test_roster_in_user_prompt(replies):
    from mnema.core.llm_client import structure_transcript
    structure_transcript("0:00 Ivanov I.I.: privet", topic="Plan",
                         roster="Ivanov I.I. — Nachal'nik otdela")
    user = replies[0][1]["content"]
    assert "Ivanov I.I. — Nachal'nik otdela" in user
    assert "SPRAVOCHNIK UCHASTNIKOV" in user


def test_no_roster_prompt_unchanged(replies):
    from mnema.core.llm_client import structure_transcript
    structure_transcript("0:00 Ivanov I.I.: privet", topic="Plan")
    user = replies[0][1]["content"]
    assert "SPRAVOCHNIK" not in user
    assert user.startswith("TEMA: Plan")


def test_roster_in_map_reduce_windows(replies, monkeypatch):
    monkeypatch.setenv("LLM_WINDOW_CHARS", "100")
    from mnema.core.llm_client import structure_transcript
    text = "\n".join(f"{i}:00 Ivanov I.I.: slovo {i} " * 3 for i in range(10))
    structure_transcript(text, topic="T", roster="Ivanov I.I. — Director")
    assert len(replies) >= 3  # neskol'ko okon + reduce
    for msgs in replies[:-1]:
        assert "Ivanov I.I. — Director" in msgs[1]["content"]


# ---- worker._llm_roster -------------------------------------------------------
def _seg(name, start):
    class S:
        pass
    s = S()
    s.speaker_name = name
    s.t_start = start
    return s


def test_llm_roster_filters_by_participants_and_position():
    from mnema.core.db import Base, set_engine_for_test
    from mnema.core.models import VoiceProfile
    from mnema.worker.tasks import _llm_roster
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        s.add(VoiceProfile(employee_name="Ivanov I.I.", position="Director"))
        s.add(VoiceProfile(employee_name="Petrov P.P.", position="Buhgalter"))
        s.add(VoiceProfile(employee_name="Sidorov S.S.", position=None))
        s.add(VoiceProfile(employee_name="Vanke V.V.", position="Bez post"))
        s.commit()
        roster = _llm_roster(s, [_seg("Ivanov I.I.", 0), _seg("Sidorov S.S.", 1),
                                 _seg("Petrov P.P.", 2), _seg(None, 3)])
    assert roster == "Ivanov I.I. — Director; Petrov P.P. — Buhgalter"


def test_llm_roster_empty_without_speakers():
    from mnema.core.db import Base, set_engine_for_test
    from mnema.worker.tasks import _llm_roster
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        assert _llm_roster(s, []) == ""
