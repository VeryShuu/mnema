"""Edinyy mehanizm uhodovyh uedomebleniy (FT-35/FT-36).

Kanal: SMTP. Ochered' pisher perezhivaet restart (tablitsa notifications
v BDD — eto SSOT; redis ispolzuetsya kak trigger workera).
Deduplikaciya: odinakovyy svoyb (odin job + odna prizna) — odno pishmo;
povtorny svoyb kategorii — cooldown ALERT_COOLDOWN_MIN, ostal'noye v digest.
Escalate: auth_session_missing — srazu, vne digest, kopiya NOTIFY_EMAIL_ADMIN.
"""
from __future__ import annotations

import datetime as dt
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable

from sqlalchemy import select

from .db import get_session
from .models import Notification

RECOMMENDED_ACTION = {
    "zoom_requires_rtms": "hост vstrechi razreshil vhod iz brauzera (ne 'tol'ko RTMS'); svyazat'sya s adminom host-akkaunta",
    "auth_session_missing": "pereloginit' Zoom-sessiyu v profile web-bot (headful login) — vsegu budushchie vstrechi pod ugrozoy",
    "waiting_room_stuck": "host ne vpustil Mnema v waiting room; dogovorit'sya o vhode ili avtopenske",
    "not_started": "vstrecha ne nachata k deadline; proavit' link i vremya starta",
    "page_crash": "padmina stranitsy/brauzera; proavit' logs konteynera web-bot",
    "timeout": "taym-aut; proavit' setevuyu dostupnost' Zoom",
}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


def parse_notify_events(spec: str | None = None) -> dict[str, str]:
    """FT-36: 'job_failed=email,protocol_ready=email' -> dict kind->channel."""
    raw = (spec if spec is not None else env("NOTIFY_EVENTS",
           "job_failed=email,session_relogin_due=email,protocol_ready=email"))
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        kind, _, channel = part.partition("=")
        out[kind.strip()] = channel.strip() or "email"
    return out


def should_notify(kind: str) -> bool:
    return parse_notify_events().get(kind) == "email"


def job_failed_subject(topic: str) -> str:
    return f"[Mnema] Ne udalos' podklyuchit'sya: {topic}"


def build_job_failed_body(*, topic: str, meeting_id: str, job_id: int,
                          attempted_at: dt.datetime | None, fail_reason: str,
                          attempts: int, job_url: str) -> str:
    """FT-35b. PWd v pishme NE ukazyvaetsya."""
    ts = (attempted_at or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    action = RECOMMENDED_ACTION.get(fail_reason, "smotretet' detali zachi")
    lines = [
        f"Vstrecha: {topic or '(bez teby)'}",
        f"meeting_id: {meeting_id}",
        f"Vremya popitki: {ts}",
        f"Prizna otkaza (fail_reason): {fail_reason}",
        f"Sdelano popitok: {attempts}",
        f"Rekomendovannoe deystvie: {action}",
        f"ID zadachi: {job_id}",
        f"Detali: {job_url}",
    ]
    return "\n".join(lines) + "\n"


class NotifyService:
    """Enqueue s dedup/cooldown + dispathch po SMTP. IDempotenten po dedup_key."""

    def __init__(self, sender: Callable[[EmailMessage], None] | None = None,
                 base_url: str | None = None):
        self._sender = sender
        self.base_url = (base_url or env("MNEMA_PUBLIC_URL", "http://localhost:8000")).rstrip("/")

    # --- enqueue -------------------------------------------------------
    def enqueue_job_failed(self, *, job_id: int, topic: str, meeting_id: str,
                           fail_reason: str, attempts: int,
                           attempted_at: dt.datetime | None = None) -> Notification | None:
        dedup = f"job_failed:{job_id}:{fail_reason}"
        now = dt.datetime.now(dt.timezone.utc)
        cooldown_min = int(env("ALERT_COOLDOWN_MIN", "60") or 60)
        with get_session() as s:
            prev = s.scalars(select(Notification).where(
                Notification.dedup_key == dedup,
            ).order_by(Notification.created_ts.desc())).first()
            if prev is not None:
                # povtornyy svoyb: cooldown; esli proshel — novoy pishmo, inache digest
                since_last = (now - prev.created_ts.replace(tzinfo=dt.timezone.utc)
                              if prev.created_ts.tzinfo is None
                              else now - prev.created_ts).total_seconds() / 60
                if since_last < cooldown_min:
                    n = Notification(
                        kind="job_failed", dedup_key=dedup,
                        subject=f"[repeat] {job_failed_subject(topic)}",
                        body="povtor svoya v predelakh cooldown — otnesён v digest",
                        to_addr="",
                        status="digest")
                    s.add(n)
                    s.commit()
                    return n
            subject = job_failed_subject(topic)
            body = build_job_failed_body(
                topic=topic, meeting_id=meeting_id, job_id=job_id,
                attempted_at=attempted_at, fail_reason=fail_reason,
                attempts=attempts, job_url=f"{self.base_url}/jobs/{job_id}")
            crit = fail_reason == "auth_session_missing"  # FT-35g
            n = Notification(
                kind="job_failed", dedup_key=dedup, subject=subject, body=body,
                to_addr=env("NOTIFY_EMAIL_TO"),
                cc_addr=(env("NOTIFY_EMAIL_ADMIN") if crit and env("NOTIFY_EMAIL_ADMIN") else None),
                status="pending")
            s.add(n)
            s.commit()
            s.refresh(n)
            return n

    def enqueue(self, kind: str, subject: str, body: str,
                dedup_key: str | None = None) -> Notification | None:
        if not should_notify(kind):
            return None
        with get_session() as s:
            if dedup_key and s.scalars(select(Notification).where(
                    Notification.dedup_key == dedup_key,
                    Notification.status.in_(("pending", "digest")))).first():
                return None
            n = Notification(kind=kind, dedup_key=dedup_key, subject=subject,
                             body=body, to_addr=env("NOTIFY_EMAIL_TO"), status="pending")
            s.add(n)
            s.commit()
            s.refresh(n)
            return n

    # --- dispatch ------------------------------------------------------
    def sender(self) -> Callable[[EmailMessage], None]:
        if self._sender is not None:
            return self._sender
        return _smtp_send

    def dispatch_pending(self, limit: int = 20) -> int:
        """Otdaet pishma-pending s retry/backoff (povtornyy vyzov otdagaet)."""
        sent = 0
        with get_session() as s:
            rows = s.scalars(select(Notification).where(
                Notification.status == "pending").limit(limit)).all()
        for n in rows:
            msg = EmailMessage()
            msg["Subject"] = n.subject
            msg["From"] = env("NOTIFY_FROM", "Mnema <mnema@localhost>")
            msg["To"] = n.to_addr
            if n.cc_addr:
                msg["Cc"] = n.cc_addr
            msg.set_content(n.body)
            try:
                self.sender()(msg)
            except Exception as e:  # noqa: BLE001
                with get_session() as s:
                    row = s.get(Notification, n.id)
                    row.attempts += 1
                    row.last_error = str(e)[:2000]
                    if row.attempts >= 5:
                        row.status = "error"
                    s.commit()
                continue
            with get_session() as s:
                row = s.get(Notification, n.id)
                row.status = "sent"
                row.attempts += 1
                row.sent_ts = dt.datetime.now(dt.timezone.utc)
                s.commit()
            sent += 1
        return sent

    def build_digest(self) -> Notification | None:
        """FT-35v: pakihs digest odnim pishmom; vyzyvaetsya po ALERT_DIGEST_CRON."""
        with get_session() as s:
            rows = s.scalars(select(Notification).where(
                Notification.status == "digest").order_by(Notification.created_ts)).all()
            if not rows:
                return None
            body_bits = ["Svedniy digest sobytiy Mnema (FT-35):", ""]
            for r in rows:
                body_bits.append(f"- {r.subject}")
                if r.body:
                    body_bits.append(r.body.rstrip())
                r.status = "sent"
                r.sent_ts = dt.datetime.now(dt.timezone.utc)
            digest = Notification(
                kind="digest", subject="[Mnema] Digest sobytiy",
                body="\n".join(body_bits) + "\n",
                to_addr=env("NOTIFY_EMAIL_TO"), status="pending")
            s.add(digest)
            s.commit()
            s.refresh(digest)
            return digest


def _smtp_send(msg: EmailMessage) -> None:
    host = env("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP_HOST ne zadan")
    port = int(env("SMTP_PORT", "587") or 587)
    user = env("SMTP_USER")
    password = _smtp_password()
    with smtplib.SMTP(host, port, timeout=30) as c:
        try:
            c.starttls(context=ssl.create_default_context())
        except smtplib.SMTPNotSupportedError:
            pass
        if user and password:
            c.login(user, password)
        c.send_message(msg)


def _smtp_password() -> str:
    """SMTP_PASSWORD_REF — ssylka na vault; v1: pryamoy SMTP_PASSWORD."""
    return env("SMTP_PASSWORD")
