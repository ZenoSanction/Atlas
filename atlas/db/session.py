"""SQLAlchemy engine and session management."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from atlas.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ATLAS ORM models."""


_engine = None
SessionLocal: sessionmaker | None = None


def init_engine() -> None:
    """Initialise the engine and session factory. Call once at startup."""
    global _engine, SessionLocal
    if _engine is not None:
        return
    s = get_settings()
    _engine = create_engine(
        s.database_url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False} if "sqlite" in s.database_url else {},
    )
    SessionLocal = sessionmaker(
        bind=_engine, autoflush=False, autocommit=False, future=True, class_=Session,
    )


def get_engine():
    if _engine is None:
        init_engine()
    return _engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context-managed session. Commits on success, rolls back on exception."""
    if SessionLocal is None:
        init_engine()
    sess = SessionLocal()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()
