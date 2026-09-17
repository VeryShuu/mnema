import datetime as dt
from email.message import EmailMessage

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mnema.core.db import Base, set_engine_for_test
from mnema.core import notify
from mnema.core.notify import (NotifyService, build_job_failed_body,
                               job_failed_subject, parse_notify_events,
                               should_notify)


@pytest.fixture()
def session(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    yield sessionmaker(bind=engine)


@pytest.fixture()
def env_base(monkeypatch):
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "ops@example.ru")
    monkeypatch.setenv("NOTIFY_EMAIL_ADMIN", "admin@example.ru")
    monkeypatch.setenv("ALERT_COOLDOWN_MIN", "60")
    monkeypatch.setenv("NOTIFY_EVENTS",
                       "job_failed=email,session_relogin_due=email,protocol_ready=email")


def test_parse_notify_events_default():
    ev = parse_notify_events("job_failed=email,protocol_ready=email")
    assert ev["job_failed"] == "email" and ev["protocol_ready"] == "email"


def test_should_notify_respects_events(monkeypatch, env_base):
    assert should_notify("job_failed")
    monkeypatch.setenv("NOTIFY_EVENTS", "protocol_ready=email")
    assert not should_notify("job_failed")


def test_build_body_has_required_fields():
    body = build_job_failed_body(
        topic="Planerka", meeting_id="9814832357", job_id=7,
        attempted_at=dt.datetime(2026, 9, 17, 10, 0, tzinfo=dt.timezone.utc),
        fail_reason="auth_session_missing", attempts=1,
        job_url="http://h/jobs/7")
    for token in ("Planerka", "9814832357", "auth_session_missing",
                  "http://h/jobs/7", "ID zadachi: 7"):
        assert token in body
    assert "pwd" not in body.lower()


def test_subject_format():
    assert job_failed_subject("X") == "[Mnema] Ne udalos' podklyuchit'sya: X"


def test_enqueue_first_is_pending(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    assert n.status == "pending"


def test_duplicate_same_reason_within_cooldown_goes_to_digest(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    a = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    b = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    assert a.status == "pending"
    assert b.status == "digest"


def test_different_reason_not_duplicate(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    a = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    b = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="not_started", attempts=1)
    assert a.status == "pending" and b.status == "pending"


def test_auth_session_missing_escalates_with_cc(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=2, topic="T", meeting_id="222",
                              fail_reason="auth_session_missing", attempts=1)
    assert n.status == "pending"
    assert n.cc_addr == "admin@example.ru"


def test_after_cooldown_new_pending(env_base, session, monkeypatch):
    monkeypatch.setenv("ALERT_COOLDOWN_MIN", "0")
    ns = NotifyService(sender=lambda m: None)
    ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                          fail_reason="timeout", attempts=1)
    b = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    assert b.status == "pending"


def test_dispatch_sends_pending(env_base, session):
    sent: list[EmailMessage] = []
    ns = NotifyService(sender=lambda m: sent.append(m))
    ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                          fail_reason="timeout", attempts=1)
    cnt = ns.dispatch_pending()
    assert cnt == 1
    assert sent and "111" in sent[0].get_payload()
    from mnema.core.models import Notification
    with session() as s:
        assert s.query(Notification).filter_by(status="sent").count() == 1


def test_dispatch_retry_keeps_pending(env_base, session):
    def boom(m):
        raise RuntimeError("smtp down")
    ns = NotifyService(sender=boom)
    ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                          fail_reason="timeout", attempts=1)
    assert ns.dispatch_pending() == 0
    from mnema.core.models import Notification
    with session() as s:
        n = s.query(Notification).first()
        assert n.status == "pending" and n.attempts == 1
        assert "smtp down" in n.last_error


def test_digest_collects_and_clears(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    for _ in range(3):
        ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    d = ns.build_digest()
    assert d is not None and d.status == "pending"
    from mnema.core.models import Notification
    with session() as s:
        assert s.query(Notification).filter_by(status="digest").count() == 0


def test_no_pwd_anywhere_in_email(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=1, topic="Sov secret",
                              meeting_id="333", fail_reason="not_started", attempts=2)
    assert "pwd" not in n.subject and "pwd" not in n.body.lower()
