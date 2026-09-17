"""Normalizaciya ssylok-priglasheniy Zoom (FT-1) + strukturnye prizey sbota (FT-5)."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# FT-5: strukturnye prizey, kotorye NE retrayutsya
STRUCTURAL_FAIL_REASONS = frozenset({
    "zoom_requires_rtms", "auth_session_missing", "waiting_room_stuck",
})

ALL_FAIL_REASONS = STRUCTURAL_FAIL_REASONS | frozenset({
    "not_started", "page_crash", "timeout",
})

_RE_DIRECT = re.compile(r"app\.zoom\.us/wc/(\d+)")
_RE_INVITE = re.compile(r"zoom\.us/j/(\d+)")


def extract_meeting_id(link_or_id: str) -> tuple[str, str | None]:
    """Vozvrachaet (meeting_id, pwd) iz ssylki-priglasheniya ili syrogo id."""
    s = link_or_id.strip()
    if re.fullmatch(r"\d{9,11}", s):
        return s, None
    full = s if "://" in s else "https://" + s
    u = urlparse(full)
    m = _RE_INVITE.search(full) or _RE_DIRECT.search(full)
    if not m:
        raise ValueError("ne raspoznan format ssylki Zoom")
    pwd = parse_qs(u.query).get("pwd") or parse_qs(u.query).get("passcode")
    return m.group(1), (pwd[0] if pwd else None)


def join_url_template(template: str, meeting_id: str, pwd: str | None) -> str:
    url = template.replace("{meeting_id}", meeting_id)
    if pwd:
        url = url.replace("{pwd}", pwd)
    else:
        url = url.replace("{pwd}", "").replace("?pwd=", "").replace("&pwd=", "")
    return url
