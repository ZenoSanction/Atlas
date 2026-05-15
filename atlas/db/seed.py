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


def initialise_database() -> None:
    """Create the database file, all tables, and minimal seed rows.

    Idempotent: running it again does nothing if the DB already has tables.
    """
    s = get_settings()
    s.ensure_directories()
    init_engine()

    # Import all model modules so SQLAlchemy registers them with Base.metadata.
    # (atlas.db.models already imported by this module.)

    engine = get_engine()
    log.info("Creating database schema at %s ...", s.database_url)
    Base.metadata.create_all(engine)

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
