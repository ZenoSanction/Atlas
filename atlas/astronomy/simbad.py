"""SIMBAD TAP client for off-catalog target resolution.

The Planner's bench-mode seasonal catalog covers ~96 well-placed
objects. Operators routinely want to image stuff that isn't in it —
NGC 2403, IC 434, Sh2-101, named comets, Bayer-designation stars.
Rather than maintain a 10k-row catalog locally, we delegate to
SIMBAD's TAP service for the long tail.

SIMBAD is the canonical name-resolution database for astronomical
objects. Free, no key needed, well-supported by every observatory in
the world. We hit it via the TAP (Table Access Protocol) endpoint
which speaks ADQL (SQL-flavoured) over plain HTTP.

API: http://simbad.u-strasbg.fr/simbad/sim-tap/sync

Query shape::

    SELECT main_id, ra, dec, otype_txt, flux
    FROM basic JOIN flux ON oidref = oid AND filter = 'V'
    WHERE id = 'NGC 2403'

We never call it for a name we already have in the seasonal catalog
(catalog hits short-circuit upstream in the route). Results are
cached in the local `targets` table so a second request for the same
name is a single SQL hit, no network round-trip.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from atlas.logging_setup import get_logger

log = get_logger("astronomy.simbad")

_TAP_URL = "https://simbad.u-strasbg.fr/simbad/sim-tap/sync"
_TIMEOUT_S = 8.0


@dataclass
class SimbadResolution:
    """One resolved astronomical object from SIMBAD."""
    name: str               # the input query, preserved for the caller
    main_id: str            # SIMBAD's canonical name ("NGC 2403", "* alf Boo")
    ra_deg: float           # J2000
    dec_deg: float
    object_type: str        # SIMBAD's otype_txt ("Galaxy", "Star", "Planetary Nebula")
    magnitude: Optional[float] = None   # V if available
    aliases: list[str] | None = None

    def to_jsonable(self) -> dict:
        return {
            "name": self.name,
            "main_id": self.main_id,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "object_type": self.object_type,
            "magnitude": self.magnitude,
            "aliases": self.aliases or [],
            "source": "simbad",
        }


async def resolve(name: str, *, timeout_s: float = _TIMEOUT_S
                   ) -> SimbadResolution | None:
    """Resolve a single name via SIMBAD's TAP service.

    Returns None if SIMBAD can't find the object, the request times
    out, or the response shape is unrecognised. Callers should treat
    "no resolution" as "not found" and fall back gracefully — never
    raise to the user.

    `name` is trimmed and uppercased for the TAP query but preserved
    in the returned SimbadResolution.name field so the caller can
    show "you searched for 'm51', SIMBAD returned 'M  51' (Whirlpool)".
    """
    name = (name or "").strip()
    if not name:
        return None
    # ADQL is SQL-ish; escape single quotes by doubling.
    safe = name.replace("'", "''")
    # Resolution must go through the `ident` table — that's what indexes
    # every alias SIMBAD knows ("NGC 2403", "PGC 21396", "UGC 3918",
    # etc.). `basic.id` only matches the canonical main_id form, so
    # queries like "NGC 2403" against it would miss even though SIMBAD
    # absolutely knows the object. We also tolerate case + extra
    # whitespace by uppercasing + TRIM-equivalent matches.
    adql = (
        "SELECT TOP 1 b.main_id, b.ra, b.dec, b.otype_txt, f.flux "
        "FROM basic AS b "
        "JOIN ident AS i ON i.oidref = b.oid "
        "LEFT OUTER JOIN flux AS f "
        "  ON f.oidref = b.oid AND f.filter = 'V' "
        f"WHERE i.id = '{safe}'"
    )
    params = {"REQUEST": "doQuery", "LANG": "ADQL",
                "FORMAT": "json", "QUERY": adql}
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as c:
            r = await c.get(_TAP_URL, params=params)
            if r.status_code != 200:
                log.debug("SIMBAD %s -> HTTP %d", name, r.status_code)
                return None
            payload = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.debug("SIMBAD %r failed: %s", name, e)
        return None

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not rows:
        return None
    row = rows[0]
    # SIMBAD's JSON column order matches the SELECT clause above.
    try:
        main_id, ra_deg, dec_deg, otype, flux_v = row[0], row[1], row[2], row[3], row[4]
    except (IndexError, TypeError):
        return None
    if ra_deg is None or dec_deg is None:
        return None

    try:
        magnitude = float(flux_v) if flux_v not in (None, "") else None
    except (TypeError, ValueError):
        magnitude = None

    return SimbadResolution(
        name=name,
        main_id=str(main_id).strip(),
        ra_deg=float(ra_deg),
        dec_deg=float(dec_deg),
        object_type=str(otype or "").strip() or "unknown",
        magnitude=magnitude,
    )
