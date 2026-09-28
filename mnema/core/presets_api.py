"""CRUD predopredelennyh ssylok Zoom (vybor iz menyu pri sozdanii vstrechi)."""
from __future__ import annotations

from sqlalchemy import select

from .db import get_session
from .models import PresetLink
from .secrets_store import delete_pwd, store_pwd
from .zoom_link import extract_meeting_id, link_valid, normalize_join_link


def list_presets() -> list[PresetLink]:
    with get_session() as s:
        return list(s.scalars(select(PresetLink).order_by(PresetLink.id)).all())


def add_preset(*, title: str, meeting_link: str,
               pwd: str | None = None) -> PresetLink:
    title = (title or "").strip()
    link = (meeting_link or "").strip()
    if not title:
        raise ValueError("нужно название пресета")
    if not link_valid(link):
        raise ValueError("ne korrektnaya ssylka Zoom")
    _, extracted_pwd = extract_meeting_id(link)
    pwd = pwd or extracted_pwd
    with get_session() as s:
        preset = PresetLink(title=title, meeting_link=normalize_join_link(link),
                            pwd_ref=store_pwd(f"preset:{title}", pwd) if pwd else None)
        s.add(preset)
        s.commit()
        s.refresh(preset)
        return preset


def delete_preset(preset_id: int) -> bool:
    with get_session() as s:
        preset = s.get(PresetLink, preset_id)
        if preset is None:
            return False
        delete_pwd(preset.pwd_ref)
        s.delete(preset)
        s.commit()
        return True


def get_preset_join(preset_id: int) -> tuple[str, str | None] | None:
    """(meeting_link, pwd) dlya sozdaniya vstrechi iz preseta."""
    with get_session() as s:
        preset = s.get(PresetLink, preset_id)
        if preset is None:
            return None
        from .secrets_store import get_pwd
        return preset.meeting_link, get_pwd(preset.pwd_ref)
