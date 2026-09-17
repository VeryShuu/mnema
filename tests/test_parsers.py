"""Unit testy razbornikov: LLM otvet -> JSON (FT-17), ASR verbose -> segmenty."""
import pytest

from mnema.core.llm_client import LLMError, _extract_json, validate_protocol, _map_reduce


VALID = ('{"topic":"T","date":"d","duration":60,"attendees":[],'
         '"agenda":[],"decisions":[],"action_items":[],"open_questions":[]}')


def test_extract_plain_json():
    assert _extract_json(VALID)["topic"] == "T"


def test_extract_markdown_fenced():
    text = "```json\n" + VALID + "\n```"
    assert _extract_json(text)["duration"] == 60


def test_extract_with_prose():
    text = "Vot protokol:\n" + VALID + "\nSpasibo!"
    assert _extract_json(text)["topic"] == "T"


def test_extract_invalid_raises():
    with pytest.raises(LLMError):
        _extract_json("nikakogo json")


def test_validate_missing_keys():
    with pytest.raises(LLMError):
        validate_protocol({"topic": "x"})


def test_validate_ok():
    d = validate_protocol(__import__("json").loads(VALID))
    assert set(d) >= {"topic", "action_items"}


def test_map_reduce_empty_partials_gives_nothing(monkeypatch):
    """Esli ni odno okno ne daet valid JSON — LLMError (ne molchal'nyy protok)."""
    import mnema.core.llm_client as lc
    def fake_chat(messages, retries=0):
        return "vse slabosť, bez json"
    monkeypatch.setattr(lc, "_chat", fake_chat)
    big = "replika " * 5000
    with pytest.raises(LLMError):
        lc._map_reduce(big, "T", 100)


def test_map_reduce_merges_partials(monkeypatch):
    import mnema.core.llm_client as lc
    part_valid = ('{"topic":"","date":"2026-09-17","duration":30,'
                  '"attendees":[{"name":"Ivan","first_seen":"00:01:00","last_seen":"00:05:00"}],'
                  '"agenda":["A"],"decisions":["D1"],"action_items":['
                  '{"task":"sdelat X","owner":"Ivan","due":"piatnitsa","source_ts":"00:02:00"}],'
                  '"open_questions":["?1"]}')

    def router(messages, retries=0):
        if "EDINY" in messages[1]["content"]:
            return VALID
        return part_valid
    monkeypatch.setattr(lc, "_chat", router)
    big = "replika " * 500
    out = lc._map_reduce(big, "Tema", 300)
    assert out["topic"] == "T"  # proshel reduce po posle edineniya okon
    big = "replika " * 500
    out = lc._map_reduce(big, "Tema", 300)
    assert out["topic"] == "T"  # proshel reduce po posle edineniya okon


def test_asr_segment_mapping_overlap():
    from mnema.core.asr_client import _map_verbose_to_segments
    raw = [{"start": 0.0, "end": 5.0, "text": "pervaya", "avg_logprob": -0.2},
           {"start": 5.2, "end": 10.0, "text": "vtoraya", "avg_logprob": -0.5}]
    vad = [{"t_start": 0.5, "t_end": 4.5}, {"t_start": 6.0, "t_end": 9.0},
           {"t_start": 50.0, "t_end": 55.0}]
    out = _map_verbose_to_segments({"segments": raw, "language": "ru"}, vad)
    assert out[0]["text"] == "pervaya"
    assert out[1]["text"] == "vtoraya"
    assert out[2]["text"] == ""


def test_conf_from_logprob_bounds():
    from mnema.core.asr_client import _conf_from_logprob
    assert _conf_from_logprob(0.0) == 1.0
    assert 0 <= _conf_from_logprob(-1.2) <= 1
