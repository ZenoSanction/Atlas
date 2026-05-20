"""Astronomy utilities — pure-Python math, no external SDKs.

For Phase 2 deeper science (precise nutation, parallax, atmospheric
refraction, polar motion) we'd swap to astropy. For tonight's
visible-target scheduler the simple formulas below are well within
the precision an amateur observatory needs (~1 arcmin)."""
from atlas.astronomy.visibility import (
    compute_alt_az, julian_date, local_sidereal_time, airmass,
    is_above_horizon, sun_ra_dec, sun_altitude, night_window,
)

__all__ = [
    "compute_alt_az", "julian_date", "local_sidereal_time",
    "airmass", "is_above_horizon", "sun_ra_dec", "sun_altitude",
    "night_window",
]
