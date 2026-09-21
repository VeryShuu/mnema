"""Podklyuchenie k BDD (SQLAlchemy). SQLite na starthe / Postgres pri roste (6.4)."""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip() or "sqlite:////data/db.sqlite"


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = database_url()
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False,
                                      "timeout": 30}
            if ":memory:" in url:
                from sqlalchemy.pool import StaticPool
                kwargs["poolclass"] = StaticPool
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite") and ":memory:" not in url:
            _init_sqlite_pragmas(_engine)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _init_sqlite_pragmas(engine) -> None:
    """WAL + busy_timeout: parallel'nyye vetki (asr/speaker) pishut odnovremenno."""
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def get_session():
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def set_engine_for_test(engine) -> None:
    global _engine, _SessionLocal
    _engine = engine
    _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
