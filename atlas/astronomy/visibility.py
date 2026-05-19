"""Visibility math for the Planner.

Pure-Python, no astropy / scipy dependency. Precision is fine for amateur
scheduling (~1 arcmin), which is what the visible-target picker needs.

For Phase 2 work that demands higher precision (precise transit timing,
sub-arcsecond astrometry), pivot to astropy.coordinates / astropy.time —
the contracts below already match those concepts.
"""
from __future__ import annotations

import math
from datetime import datetime


def julian_date(when_utc: datetime) -> float:
    """Astronomical Julian Date for a UTC datetime.
    Meeus, Astronomical Algorithms 2nd ed., chapter 7."""
    y = when_utc.year
    m = when_utc.month
    d = (when_utc.day
         + (when_utc.hour
             + (when_utc.minute
                + (when_utc.second + when_utc.microsecond / 1e6) / 60.0) / 60.0) / 24.0)
    if m <= 2:
        y -= 1
        m += 12
    a = math.floor(y / 100)
    b = 2 - a + math.floor(a / 4)
    jd = (math.floor(365.25 * (y + 4716))
            + math.floor(30.6001 * (m + 1))
            + d + b - 1524.5)
    return jd


def local_sidereal_time(when_utc: datetime, longitude_deg: float) -> float:
    """Local apparent sidereal time, in degrees, for the given UTC and east-longitude.
    Approximate (~1 arcsec/century unmodelled nutation)."""
    jd = julian_date(when_utc)
    t = (jd - 2451545.0) / 36525.0
    # Mean sidereal time at Greenwich (degrees)
    gst = (280.46061837
            + 360.98564736629 * (jd - 2451545.0)
            + 0.000387933 * t * t
            - t * t * t / 38710000.0)
    gst = gst % 360.0
    return (gst + longitude_deg) % 360.0


def compute_alt_az(ra_deg: float, dec_deg: float,
                    latitude_deg: float, longitude_deg: float,
                    when_utc: datetime | None = None) -> tuple[float, float]:
    """Return (altitude_deg, azimuth_deg) for a celestial position from the
    given site at the given UTC. Longitude is east-positive.

    Azimuth is measured clockwise from north (0=N, 90=E, 180=S, 270=W).
    """
    if when_utc is None:
        when_utc = datetime.utcnow()
    lst = local_sidereal_time(when_utc, longitude_deg)
    ha_deg = (lst - ra_deg + 360.0) % 360.0     # hour angle, degrees
    ha = math.radians(ha_deg)
    dec = math.radians(dec_deg)
    lat = math.radians(latitude_deg)

    sin_alt = (math.sin(dec) * math.sin(lat)
                + math.cos(dec) * math.cos(lat) * math.cos(ha))
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))

    # Azimuth from north, clockwise
    cos_az = ((math.sin(dec) - math.sin(alt) * math.sin(lat))
              / (math.cos(alt) * math.cos(lat)))
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.acos(cos_az)
    if math.sin(ha) > 0:
        az = 2 * math.pi - az
    return math.degrees(alt), math.degrees(az)


def airmass(altitude_deg: float) -> float | None:
    """Plane-parallel airmass approximation (sec z). Returns None below horizon.
    Good to ~1% for altitudes > 20°. For more accuracy below 20°, use
    Pickering's 2002 formula."""
    if altitude_deg <= 0:
        return None
    z = math.radians(90.0 - altitude_deg)
    return 1.0 / max(math.cos(z), 1e-6)


def is_above_horizon(ra_deg: float, dec_deg: float,
                     latitude_deg: float, longitude_deg: float,
                     min_altitude_deg: float = 20.0,
                     when_utc: datetime | None = None) -> bool:
    alt, _ = compute_alt_az(ra_deg, dec_deg, latitude_deg, longitude_deg, when_utc)
    return alt >= min_altitude_deg
