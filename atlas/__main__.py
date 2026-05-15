"""ATLAS command-line entry point.

Usage:
    python -m atlas              # start the server (default)
    python -m atlas serve        # start the server explicitly
    python -m atlas init-db      # initialise the database
    python -m atlas version      # print version
"""
from __future__ import annotations

import argparse
import sys

from atlas import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="ATLAS — Autonomous Telescope & Learning Astronomy System",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Start the ATLAS server (default)")
    sub.add_parser("init-db", help="Initialise the ATLAS database")
    sub.add_parser("version", help="Print the ATLAS version")

    args = parser.parse_args(argv)

    cmd = args.command or "serve"

    if cmd == "version":
        print(f"ATLAS {__version__}")
        return 0

    if cmd == "init-db":
        from atlas.db.seed import initialise_database
        initialise_database()
        return 0

    if cmd == "serve":
        # Import here so `--help` and `version` don't pay the import cost
        import uvicorn
        from atlas.config import get_settings
        s = get_settings()
        uvicorn.run(
            "atlas.server:app",
            host=s.server_host,
            port=s.server_port,
            log_config=None,  # ATLAS configures its own logging
            access_log=False,
        )
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
