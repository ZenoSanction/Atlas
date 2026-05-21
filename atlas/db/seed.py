"""Database initialisation and seed data."""
from __future__ import annotations

from datetime import datetime

from atlas import __version__
from atlas.config import get_settings
from atlas.db.models import (
    NotificationConfig, RetentionPolicy, VersionInfo,
)
from atlas.db.session import Base, get_engine, get_session, init_engine
from atlas.logging_setup import get_logger

log = get_logger("db.seed")

SCHEMA_VERSION = "1.0.0"


def _apply_simple_migrations(engine) -> None:
    """Add columns that have been added to models since the original
    install. SQLAlchemy's create_all only creates missing TABLES, not
    missing COLUMNS on existing tables. SQLite's ALTER TABLE supports
    only ADD COLUMN, which is enough for additive schema evolution.

    Each tuple: (table_name, column_name, sqlite_type, sqlite_default_sql).
    If the column already exists, skipped silently. This is intentionally
    a hand-maintained list rather than a generic differ — Phase 2 schema
    is small enough that this keeps it predictable and easy to review."""
    additive_columns = [
        ("equipment_profile", "capture_folder", "VARCHAR(512)", "NULL"),
    ]
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, col, coltype, default_sql in additive_columns:
            cols = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            if col in cols:
                continue
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype} DEFAULT {default_sql}"))
                log.info("Migrated: ALTER TABLE %s ADD COLUMN %s %s",
                         table, col, coltype)
            except Exception as e:
                log.warning("Skipped migration on %s.%s: %s", table, col, e)


def initialise_database() -> None:
    """Create the database file, all tables, and minimal seed rows.

    Idempotent: running it again does nothing if the DB already has tables.
    Also applies any pending additive column migrations to existing tables.
    """
    s = get_settings()
    s.ensure_directories()
    init_engine()

    # Import all model modules so SQLAlchemy registers them with Base.metadata.
    # (atlas.db.models already imported by this module.)

    engine = get_engine()
    log.info("Creating database schema at %s ...", s.database_url)
    Base.metadata.create_all(engine)
    _apply_simple_migrations(engine)

    with get_session() as sess:
        if sess.query(VersionInfo).first() is None:
            sess.add(VersionInfo(
                schema_version=SCHEMA_VERSION,
                atlas_version=__version__,
                installed_at=datetime.utcnow(),
            ))
            log.info("Stamped schema_version=%s atlas_version=%s",
                     SCHEMA_VERSION, __version__)

        if sess.query(RetentionPolicy).first() is None:
            sess.add(RetentionPolicy())
            log.info("Seeded default retention policy.")

        if sess.query(NotificationConfig).first() is None:
            sess.add(NotificationConfig())
            log.info("Seeded default notification config.")

    log.info("Database initialisation complete.")


if __name__ == "__main__":
    from atlas.logging_setup import setup_logging
    setup_logging(level="INFO", log_dir=get_settings().logs_dir)
    initialise_database()
