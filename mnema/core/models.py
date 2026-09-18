"""Modeli BDD po TZ 6.2."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

JOB_STATUSES = ("scheduled", "launching", "recording", "processing",
                "awaiting_llm", "awaiting_review", "done", "failed", "cancelled")
SPEAKER_SOURCES = ("voiceprint", "unresolved", "multi_voice")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zoom_meeting_id: Mapped[str] = mapped_column(String(64), index=True)
    join_link: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    pwd_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    topic: Mapped[str] = mapped_column(String(512), default="")
    start_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_max_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="api")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    join_attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    review_deadline: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    events: Mapped[list["SessionEvent"]] = relationship(back_populates="job")
    audio_files: Mapped[list["AudioFile"]] = relationship(back_populates="job")
    segments: Mapped[list["Segment"]] = relationship(back_populates="job")
    protocol: Mapped["Protocol | None"] = relationship(back_populates="job", uselist=False)


class SessionEvent(Base):
    __tablename__ = "session_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    event: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped[Job] = relationship(back_populates="events")


class AudioFile(Base):
    __tablename__ = "audio_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # wav | webm | mp3
    path: Mapped[str] = mapped_column(String(1024))
    bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="audio_files")


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    speaker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # diar cluster label
    speaker_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    speaker_source: Mapped[str] = mapped_column(String(16), default="unresolved")
    diar_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_track: Mapped[str | None] = mapped_column(String(64), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    t_start: Mapped[float] = mapped_column(Float)
    t_end: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text, default="")
    lang: Mapped[str | None] = mapped_column(String(8), nullable=True)
    engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    job: Mapped[Job] = relationship(back_populates="segments")


class Protocol(Base):
    __tablename__ = "protocols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True, index=True)
    json_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    md_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    docx_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    steno_md_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="protocol")


class VoiceProfile(Base):
    __tablename__ = "voice_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_name: Mapped[str] = mapped_column(String(255))
    employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    sample_paths: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    consent_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    match_stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ReviewRequest(Base):
    __tablename__ = "review_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    cluster_label: Mapped[str] = mapped_column(String(64))
    candidate_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_profiles.id"), nullable=True)
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    answered_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answered_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionHealth(Base):
    __tablename__ = "session_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checked_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Notification(Base):
    """Ochered'/zhurnal uedomebleniy (FT-35): zhivёt restartz (v BDD i redis)."""
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)  # job_failed|protocol_ready|...
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    subject: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    to_addr: Mapped[str] = mapped_column(String(255))
    cc_addr: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|sent|digest|suppressed|error
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sent_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
