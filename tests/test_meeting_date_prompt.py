"""Testy peredachi daty sovieshchaniya v LLM-prompt (real'nyye sroki porucheniy).

DATA SOVESHCHEANIYA dolzhna popast' v user-soobshchenie (odin okno i map-reduce),
bez nei prompt ne menyayetsya; pustom date v protokole zamolyaetsya fakticheskoy
dataoy vstrechi."""
import json
import os

import pytest

os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive-mdate")
os.environ.setdefault("DATA_DIR", "/tmp/test-data-mdate")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-mdate.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")


def _protocol_json(**overrides):
    d = {"topic": "T", "date": "", "duration": 0, "attendees": [],
         "agenda": [], "decisions": [], "action_items": [],
         "open_questions": []}
    d.update(overrides)
    return json.dumps(d, ensure_ascii=False)


@pytest.fixture()
def replies(monkeypatch):
    """Lovit soobshcheniya; otvet — JSON protokola iz state['content'].

    Chitat' content pri kazhdom zaprose, chtoby test mog podmenit' otvet
    (napr. s zapolnennoy datey) do ili miedu vysovy."""
    calls = []

    state = {"content": [_protocol_json()]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["messages"])
        idx = min(len(calls) - 1, len(state["content"]) - 1)
        content = state["content"][idx]

        class R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": content}}]}
        return R()

    monkeypatch.setattr("httpx.post", fake_post)
    return calls


def test_meeting_date_in_user_prompt(replies):
    from mnema.core.llm_client import structure_transcript
    structure_transcript("0:00 Ivanov: sdelat' do pyatnicy", topic="Plan",
                         meeting_date="23.10.2026")
    user = replies[0][1]["content"]
    assert "DATA SOVESHCHEANIYA: 23.10.2026" in user
    assert user.index("DATA SOVESHCHEANIYA") < user.index("TEMA: Plan")


def test_no_meeting_date_prompt_unchanged(replies):
    from mnema.core.llm_client import structure_transcript
    structure_transcript("0:00 Ivanov: privet", topic="Plan")
    user = replies[0][1]["content"]
    assert "DATA SOVESHCHEANIYA" not in user
    assert user.startswith("TEMA: Plan")


def test_system_prompt_requires_real_dates():
    from mnema.core.llm_client import SYSTEM_PROMPT
    assert "DD.MM.YYYY" in SYSTEM_PROMPT
    assert "DATA SOVESHCHEANIYA" in SYSTEM_PROMPT


def test_meeting_date_in_map_reduce_windows_and_reduce(replies, monkeypatch):
    monkeypatch.setenv("LLM_WINDOW_CHARS", "100")
    from mnema.core.llm_client import structure_transcript
    text = "\n".join(f"{i}:00 Ivanov: slovo {i} " * 3 for i in range(10))
    structure_transcript(text, topic="T", meeting_date="23.10.2026")
    assert len(replies) >= 3  # neskol'ko okon + reduce
    for msgs in replies:
        assert "DATA SOVESHCHEANIYA: 23.10.2026" in msgs[1]["content"]


def test_empty_date_backfilled_from_meeting_date(replies):
    from mnema.core.llm_client import structure_transcript
    data = structure_transcript("0:00 Ivanov: privet", topic="T",
                                meeting_date="23.10.2026")
    assert data["date"] == "23.10.2026"


def test_llm_date_preserved_when_given(monkeypatch):
    from mnema.core.llm_client import structure_transcript

    def fake_post(url, headers=None, json=None, timeout=None):
        class R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content":
                        _protocol_json(date="01.01.2026")}}]}
        return R()

    monkeypatch.setattr("httpx.post", fake_post)
    data = structure_transcript("x", topic="T", meeting_date="23.10.2026")
    assert data["date"] == "01.01.2026"
