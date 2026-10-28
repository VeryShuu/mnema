import pytest

from mnema.core.zoom_link import (STRUCTURAL_FAIL_REASONS,
                                  extract_meeting_id, join_url_template)


def test_plain_id():
    assert extract_meeting_id("9814832357") == ("9814832357", None)


def test_invite_link_with_pwd():
    mid, pwd = extract_meeting_id("https://us02web.zoom.us/j/9814832357?pwd=abc123")
    assert mid == "9814832357" and pwd == "abc123"


def test_invite_link_passcode_param():
    _, pwd = extract_meeting_id("https://us05web.zoom.us/j/123456789?passcode=xyz")
    assert pwd == "xyz"


def test_pmn_link():
    mid, pwd = extract_meeting_id("https://us06web.zoom.us/my/corp.gold?pwd=abc.1")
    assert mid == "my/corp.gold" and pwd == "abc.1"


def test_direct_wc_link():
    mid, _ = extract_meeting_id("https://app.zoom.us/wc/987654321/join")
    assert mid == "987654321"


def test_link_without_scheme():
    mid, pwd = extract_meeting_id("us04web.zoom.us/j/1112223334?pwd=zz")
    assert mid == "1112223334" and pwd == "zz"


def test_garbage_raises():
    with pytest.raises(ValueError):
        extract_meeting_id("https://example.com/meet")


def test_template_with_pwd():
    t = "https://app.zoom.us/wc/{meeting_id}/join?pwd={pwd}"
    assert join_url_template(t, "123", "p") == "https://app.zoom.us/wc/123/join?pwd=p"


def test_template_without_pwd_strips_param():
    t = "https://app.zoom.us/wc/{meeting_id}/join?pwd={pwd}"
    assert join_url_template(t, "123", None) == "https://app.zoom.us/wc/123/join"


def test_structural_reasons_match_tz():
    assert STRUCTURAL_FAIL_REASONS == {
        "zoom_requires_rtms", "auth_session_missing", "waiting_room_stuck"}

from mnema.core.zoom_link import link_valid


def test_link_valid_accepts_examples():
    assert link_valid("https://us06web.zoom.us/j/123456789?pwd=abc.1")
    assert link_valid("9814832357")
    assert link_valid("zoom.us/my/john.doe-1")


def test_link_valid_rejects_garbage():
    assert not link_valid("")
    assert not link_valid("   ")
    assert not link_valid("hello world")
    assert not link_valid("https://example.com/j/123456789")
