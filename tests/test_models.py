from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mnema.core.db import Base
import mnema.core.models as m


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)
    with sm() as s:
        yield s


def test_create_job_with_relations(session):
    job = m.Job(zoom_meeting_id="9814832357", topic="Planerka",
                status="scheduled")
    session.add(job)
    session.flush()
    session.add(m.SessionEvent(job_id=job.id, event="navigating"))
    session.add(m.Segment(job_id=job.id, t_start=0.0, t_end=2.5,
                          text="pervaya replika", speaker_source="unresolved"))
    session.add(m.AudioFile(job_id=job.id, kind="webm", path="/data/x.raw.webm"))
    session.commit()
    job_id = job.id

    session.expire_all()
    j = session.get(m.Job, job_id)
    assert j.topic == "Planerka"
    assert len(j.events) == 1 and len(j.segments) == 1 and len(j.audio_files) == 1
    assert j.status == "scheduled"


def test_protocol_one_to_one_job(session):
    job = m.Job(zoom_meeting_id="1", status="processing")
    session.add(job)
    session.flush()
    p = m.Protocol(job_id=job.id, json_data={"topic": "T"}, llm_model="m1")
    session.add(p)
    session.commit()
    assert session.get(m.Job, job.id).protocol.json_data["topic"] == "T"


def test_pwd_not_in_job_repr(session):
    job = m.Job(zoom_meeting_id="1", pwd_ref="pwdref:deadbeef")
    session.add(job)
    session.commit()
    assert "pwd=" not in repr(job)


def test_voice_profile_and_review(session):
    vp = m.VoiceProfile(employee_name="Ivan Petrov", embedding=[0.1] * 4,
                        consent_ts=datetime.now(timezone.utc))
    session.add(vp)
    session.flush()
    job = m.Job(zoom_meeting_id="2")
    session.add(job)
    session.flush()
    rr = m.ReviewRequest(job_id=job.id, cluster_label="Golos 1",
                         candidate_profile_id=vp.id, similarity=0.71)
    session.add(rr)
    session.commit()
    assert rr.status == "pending"


def test_utc_timestamps_default(session):
    job = m.Job(zoom_meeting_id="3")
    session.add(job)
    session.commit()
    assert job.created_ts is not None
