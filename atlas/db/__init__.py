"""ATLAS database layer.

Exports:
    Base                    SQLAlchemy declarative base
    SessionLocal            session factory
    get_session()           dependency-injection helper for FastAPI
    init_engine()           initialise the engine + session factory
"""
from atlas.db.session import Base, SessionLocal, get_session, init_engine

__all__ = ["Base", "SessionLocal", "get_session", "init_engine"]
