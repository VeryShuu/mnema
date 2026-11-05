"""Unit testy koalescencii stenogrammy: pustye reperiki i povtory odnogo golosa."""
from types import SimpleNamespace

from mnema.core.coalesce import (DisplaySegment, coalesce_segments,
                                 expand_segments, speaker_display)


def _seg(t0, t1, text, speaker_id="SPEAKER_00", speaker_name=None, needs_review=False,
         speaker_source=None):
    return SimpleNamespace(t_start=t0, t_end=t1, text=text, speaker_id=speaker_id,
                           speaker_name=speaker_name, needs_review=needs_review,
                           speaker_source=speaker_source)


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


def test_two_voices_flag_from_multi_voice_source():
    """multi_voice segmenny (dva golosa v fragmente) — dvukhgolosy, ne 'utochnenie'."""
    out = coalesce_segments([_seg(0, 5, "a", "S0", speaker_source="multi_voice",
                                  needs_review=True)])
    assert out[0].two_voices is True


def test_two_voices_propagates_on_merge():
    segs = [_seg(0, 5, "a", "S0", speaker_source="multi_voice"),
            _seg(5, 10, "b", "S0")]
    out = coalesce_segments(segs)
    assert len(out) == 1 and out[0].two_voices is True


def test_two_voices_default_false_for_plain_segments():
    out = coalesce_segments([_seg(0, 5, "a", "S0", needs_review=True)])
    assert out[0].two_voices is False and out[0].needs_review is True


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


# ---- razbienie VAD-stroki s dvumya golosami (turn boundaries) -------------

def _asr_seg(t0, t1, text, parts, diar_label="SPEAKER_00", speaker_id=None,
             speaker_name=None, needs_review=False):
    return SimpleNamespace(t_start=t0, t_end=t1, text=text, parts=None,
                           asr_json=parts, speaker_id=speaker_id or diar_label,
                           diar_label=diar_label, speaker_name=speaker_name,
                           needs_review=needs_review)


TURNS_AB = [[0.0, 9.0, "SPEAKER_00"], [9.0, 15.0, "SPEAKER_01"]]


def _two_voice_line():
    return _asr_seg(0.0, 15.0, "pervaya fraza vtornaya fraza",
                    [{"s": 0.5, "e": 4.0, "t": "pervaya fraza"},
                     {"s": 4.5, "e": 8.5, "t": "toze pervogo"},
                     {"s": 9.4, "e": 14.5, "t": "vtornaya fraza"}])


def test_expand_splits_line_with_two_voices():
    out = expand_segments([_two_voice_line()], TURNS_AB)
    assert len(out) == 2
    assert out[0].speaker_id == "SPEAKER_00"
    assert out[1].speaker_id == "SPEAKER_01"
    assert out[0].text == "pervaya fraza toze pervogo"
    assert out[1].text == "vtornaya fraza"


def test_expand_piece_times_come_from_asr_parts():
    out = expand_segments([_two_voice_line()], TURNS_AB)
    assert (out[0].t_start, out[0].t_end) == (0.5, 8.5)
    assert (out[1].t_start, out[1].t_end) == (9.4, 14.5)


def test_expand_second_speaker_unnamed_needs_review():
    out = expand_segments([_two_voice_line()], TURNS_AB)
    assert out[1].speaker_name is None
    assert out[1].needs_review is True
    assert speaker_display(out[1]) == "Голос SPEAKER_01"


def test_expand_uses_resident_name_of_other_cluster():
    """Imya vtorogo golosa beretsya iz drugogo segmenta togo zhe klastera."""
    other = _seg(20.0, 25.0, "ok", "SPEAKER_01", "Petrov")
    out = expand_segments([_two_voice_line(), other], TURNS_AB)
    piece = out[1]
    assert piece.speaker_name == "Petrov"
    assert piece.needs_review is False
    assert speaker_display(piece) == "Petrov"


def test_expand_keeps_line_with_single_voice():
    seg = _asr_seg(0.0, 10.0, "a b", [{"s": 0.5, "e": 4.0, "t": "a"},
                                       {"s": 5.0, "e": 9.0, "t": "b"}])
    turns = [[0.0, 10.0, "SPEAKER_00"]]
    out = expand_segments([seg], turns)
    assert out == [seg]


def test_expand_without_turns_passthrough():
    line = _two_voice_line()
    out = expand_segments([line], None)
    assert out == [line]


def test_expand_disabled_env_passthrough(monkeypatch):
    monkeypatch.setenv("COALESCE_SPLIT_TURNS", "0")
    line = _two_voice_line()
    assert expand_segments([line], TURNS_AB) == [line]


def test_expand_tiny_overlap_not_cut():
    """Domina chuzhogo golosa na chasti nizhe poroga — chast' ostaetsya baze."""
    seg = _asr_seg(0.8, 20.0, "a b", [{"s": 1.0, "e": 4.0, "t": "a"},
                                       {"s": 10.0, "e": 20.0, "t": "b"}])
    # na [10,20] dominante S1 pokryvaet tol'ko 1 s iz 10 (ratio 0.1 < 0.35)
    turns = [[0.0, 4.5, "SPEAKER_00"], [11.0, 12.0, "SPEAKER_01"],
             [15.0, 16.0, "SPEAKER_02"]]
    out = expand_segments([seg], turns)
    assert out == [seg]


def test_expand_line_without_parts_untouched():
    """Starye joby bez asr_json: stroka kak byla."""
    seg = _seg(0.0, 15.0, "staraya stroka", "S0")
    assert expand_segments([seg], TURNS_AB) == [seg]


def test_expand_then_coalesce_keeps_voices_separate():
    """Regressiya glavnogo defekta: dve frazy dvuh lyudey v odnoy VAD-stroke
    posle vyvoda — dve otdel'nyye reperiki s raznymi govorashchimi."""
    other = _seg(16.0, 20.0, "eto tozhe ya", "SPEAKER_01", "Petrov")
    disp = coalesce_segments(expand_segments([_two_voice_line(), other], TURNS_AB))
    assert [d.speaker_name for d in disp] == [None, "Petrov"]

