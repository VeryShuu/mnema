"""Unit testy koalescencii stenogrammy: pustye reperiki i povtory odnogo golosa."""
from types import SimpleNamespace

from mnema.core.coalesce import DisplaySegment, coalesce_segments, speaker_display


def _seg(t0, t1, text, speaker_id="SPEAKER_00", speaker_name=None, needs_review=False):
    return SimpleNamespace(t_start=t0, t_end=t1, text=text, speaker_id=speaker_id,
                           speaker_name=speaker_name, needs_review=needs_review)


# ---- pustye reperiki ------------------------------------------------------

def test_drop_empty_text_segment():
    out = coalesce_segments([_seg(0, 5, ""), _seg(5, 10, "est' tekst")])
    assert [d.text for d in out] == ["est' tekst"]


def test_drop_whitespace_only_segment():
    out = coalesce_segments([_seg(0, 5, "   \n\t ")])
    assert out == []


def test_keep_all_text_segments():
    out = coalesce_segments([_seg(0, 5, "a", "S0"), _seg(5, 10, "b", "S1")])
    assert [d.text for d in out] == ["a", "b"]


# ---- sliyanie odnogo golosa ----------------------------------------------

def test_merge_consecutive_same_speaker():
    segs = [_seg(0, 15, "pervaya", "S0", "P"), _seg(15, 30, "vtoraya", "S0", "P")]
    out = coalesce_segments(segs)
    assert len(out) == 1
    assert out[0].text == "pervaya vtoraya"


def test_merge_times_span_first_to_last():
    segs = [_seg(125.0, 140.0, "a", "S0"), _seg(140.0, 152.5, "b", "S0")]
    out = coalesce_segments(segs)
    assert (out[0].t_start, out[0].t_end) == (125.0, 152.5)


def test_no_merge_different_speakers():
    segs = [_seg(0, 5, "moya", "S0"), _seg(5, 10, "tvoya", "S1")]
    out = coalesce_segments(segs)
    assert [d.speaker_id for d in out] == ["S0", "S1"]


def test_no_merge_different_names_same_cluster():
    segs = [_seg(0, 5, "a", "S0", "Ivanov"), _seg(5, 10, "b", "S0", "Petrov")]
    out = coalesce_segments(segs)
    assert [d.speaker_name for d in out] == ["Ivanov", "Petrov"]


def test_merge_same_name_different_clusters():
    """Posle utochnenija imeni raznye klastery — tot zhe chelovek: sliyaem."""
    segs = [_seg(0, 5, "a", "SPEAKER_00", "Ivanov"),
            _seg(5, 10, "b", "SPEAKER_01", "Ivanov")]
    out = coalesce_segments(segs)
    assert len(out) == 1 and out[0].text == "a b"


def test_merge_bridges_empty_segment_of_same_speaker():
    segs = [_seg(0, 5, "slovo", "S0"), _seg(5, 20, "", "S0"),
            _seg(20, 25, "eshcho", "S0")]
    out = coalesce_segments(segs)
    assert len(out) == 1 and out[0].text == "slovo eshcho"


# ---- zazor ---------------------------------------------------------------

def test_no_merge_beyond_gap_limit():
    segs = [_seg(0, 5, "ran'she", "S0"), _seg(400, 405, "pozzhe", "S0")]
    out = coalesce_segments(segs)
    assert len(out) == 2


def test_gap_limit_configurable(monkeypatch):
    monkeypatch.setenv("COALESCE_MAX_GAP_S", "1000")
    segs = [_seg(0, 5, "a", "S0"), _seg(400, 405, "b", "S0")]
    assert len(coalesce_segments(segs)) == 1


# ---- flagi i imena -------------------------------------------------------

def test_merge_propagates_needs_review():
    segs = [_seg(0, 5, "chisto", "S0"),
            _seg(5, 10, "somnitel'no", "S0", needs_review=True)]
    out = coalesce_segments(segs)
    assert out[0].needs_review is True


def test_display_name_fallback_and_priority():
    assert speaker_display(_seg(0, 1, "x", "SPEAKER_01")) == "Голос SPEAKER_01"
    named = _seg(0, 1, "x", "SPEAKER_01", "Ivanov I. I.")
    assert speaker_display(named) == "Ivanov I. I."


def test_display_name_fallback_missing_id():
    assert speaker_display(_seg(0, 1, "x", None)) == "Голос ?"


def test_returned_items_are_display_segments():
    out = coalesce_segments([_seg(0, 5, "a")])
    assert isinstance(out[0], DisplaySegment)


def test_empty_input():
    assert coalesce_segments([]) == []
