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
_RE_PMN = re.compile(r"zoom\.us/my/([\w.\-]+)")


def extract_meeting_id(link_or_id: str) -> tuple[str, str | None]:
    """Vozvrachaet (meeting_id, pwd) iz ssylki-priglasheniya ili syrogo id.

    Dlya PMN-ssilok (zoom.us/my/<name>) meeting_id = 'my/<name>'.
    """
    s = link_or_id.strip()
    if re.fullmatch(r"\d{9,11}", s):
        return s, None
    full = s if "://" in s else "https://" + s
    u = urlparse(full)
    m = _RE_INVITE.search(full) or _RE_DIRECT.search(full)
    if m:
        mid = m.group(1)
    else:
        mp = _RE_PMN.search(full)
        if not mp:
            raise ValueError("Не распознан формат ссылки Zoom")
        mid = "my/" + mp.group(1)
    pwd = parse_qs(u.query).get("pwd") or parse_qs(u.query).get("passcode")
    return mid, (pwd[0] if pwd else None)


def link_valid(candidate: str) -> bool:
    """True esli strokeka - korektnaya ssylka Zoom ili syroi ID vstrechi."""
    try:
        extract_meeting_id(candidate or "")
        return True
    except ValueError:
        return False


def normalize_join_link(link: str) -> str:
    """Ssylka bez parolya dlya hrineniya (pwd - v secrets store)."""
    if not link or "://" not in link:
        return link
    u = urlparse(link)
    q = [(k, v) for k, v in parse_qs(u.query).items() if k not in ("pwd", "passcode")]
    from urllib.parse import urlencode
    new_q = urlencode(dict(q))
    return u._replace(query=new_q).geturl()


def join_url_template(template: str, meeting_id: str, pwd: str | None) -> str:
    url = template.replace("{meeting_id}", meeting_id)
    if pwd:
        url = url.replace("{pwd}", pwd)
    else:
        url = url.replace("{pwd}", "").replace("?pwd=", "").replace("&pwd=", "")
    return url
