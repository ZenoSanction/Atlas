"""ATLAS command-line entry point.

Usage:
    python -m atlas              # start the server (default)
    python -m atlas serve        # start the server explicitly
    python -m atlas serve --https
                                 # start with self-signed HTTPS so the
                                 # microphone works from LAN devices
                                 # (Chrome blocks getUserMedia on plain
                                 # http://<lan-ip> origins)
    python -m atlas init-db      # initialise the database
    python -m atlas regen-cert   # regenerate the self-signed TLS cert
    python -m atlas version      # print version
"""
from __future__ import annotations

import argparse
import os
import sys

# Switch stdout/stderr to UTF-8 immediately on Windows so the boot banner
# (and every subsequent log line) can carry non-ASCII characters without
# crashing the cp1252 console.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from atlas import __version__


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="ATLAS — Autonomous Telescope & Learning Astronomy System",
    )
    sub = parser.add_subparsers(dest="command")

    serve_p = sub.add_parser("serve", help="Start the ATLAS server (default)")
    serve_p.add_argument(
        "--https", action="store_true",
        help="Serve over HTTPS using a self-signed certificate "
              "(required for microphone access from LAN devices). "
              "Also enabled when ATLAS_HTTPS=1 is set in the environment.",
    )
    serve_p.add_argument(
        "--no-https", action="store_true",
        help="Force plain HTTP even if ATLAS_HTTPS is set.",
    )

    sub.add_parser("init-db", help="Initialise the ATLAS database")
    sub.add_parser(
        "regen-cert",
        help="Regenerate the self-signed TLS cert. Pick this up next "
              "time you start the server. Run this after the host's IP "
              "changes if the warm-room URL stops matching the cert.",
    )
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

    if cmd == "regen-cert":
        from atlas.tls import generate_cert
        info = generate_cert(force=True)
        print(f"Regenerated self-signed TLS cert.")
        print(f"  Path:        {info.cert_path}")
        print(f"  Fingerprint: {info.fingerprint_sha256}")
        print(f"  Valid until: {info.not_after}")
        print(f"  Covers:      {', '.join(info.subject_alt_names)}")
        return 0

    if cmd == "serve":
        import uvicorn
        from atlas.config import get_settings
        s = get_settings()

        # Resolve HTTPS preference: --no-https beats --https beats env var.
        use_https = (
            (args.https or _env_truthy("ATLAS_HTTPS"))
            and not args.no_https
        )

        ssl_kwargs = {}
        info = None
        if use_https:
            from atlas.tls import ensure_cert
            info = ensure_cert()
            ssl_kwargs = {
                "ssl_keyfile": str(info.key_path),
                "ssl_certfile": str(info.cert_path),
            }

        scheme = "https" if use_https else "http"
        print()
        print(f"  ATLAS {__version__} starting on {scheme}://{s.server_host}:{s.server_port}")
        if use_https and info is not None:
            from atlas.tls import discover_local_ips
            urls = [f"{scheme}://localhost:{s.server_port}"]
            for ip in discover_local_ips():
                if ip == "127.0.0.1":
                    continue
                urls.append(f"{scheme}://{ip}:{s.server_port}")
            print()
            print("  Reach the dashboard from:")
            for u in urls:
                print(f"    {u}")
            print()
            print("  Self-signed cert - your browser will warn once per device.")
            print("  Click Advanced -> Proceed; the warning won't reappear.")
            print(f"  Cert SHA-256: {info.fingerprint_sha256}")
            print(f"  Valid until:  {info.not_after}")
        print()

        uvicorn.run(
            "atlas.server:app",
            host=s.server_host,
            port=s.server_port,
            log_config=None,  # ATLAS configures its own logging
            access_log=False,
            **ssl_kwargs,
        )
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
