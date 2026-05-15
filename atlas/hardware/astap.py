"""ASTAP plate solver wrapper.

ASTAP is a free, fast, offline plate solver. We invoke it as a subprocess
and parse the .ini result file it produces alongside the input FITS.

    astap -f image.fits -ra <hours> -spd <90+dec_deg> -r <radius_deg> -fov <fov_deg>

Returns a WCS dict on success.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from atlas.logging_setup import get_logger

log = get_logger("hardware.astap")


class AstapError(RuntimeError):
    pass


class AstapClient:
    def __init__(self, astap_path: Path | str | None = None) -> None:
        self._path = Path(astap_path) if astap_path else None

    async def solve(self, fits_path: Path | str, *,
                    ra_hours: float | None = None,
                    dec_deg: float | None = None,
                    fov_deg: float | None = None,
                    radius_deg: float = 5.0,
                    timeout_s: float = 120.0) -> dict:
        """Plate-solve a FITS image. Returns a WCS dict on success."""
        if self._path is None:
            raise AstapError("ASTAP path not configured")
        fits_path = Path(fits_path)
        if not fits_path.exists():
            raise AstapError(f"FITS not found: {fits_path}")

        cmd = [str(self._path), "-f", str(fits_path), "-r", str(radius_deg)]
        if ra_hours is not None:
            cmd += ["-ra", f"{ra_hours:.6f}"]
        if dec_deg is not None:
            cmd += ["-spd", f"{90.0 + dec_deg:.6f}"]
        if fov_deg is not None:
            cmd += ["-fov", f"{fov_deg:.6f}"]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise AstapError(f"ASTAP timed out after {timeout_s}s")

        if proc.returncode != 0:
            raise AstapError(
                f"ASTAP failed (exit {proc.returncode}): "
                f"{stderr.decode('utf-8', errors='ignore')[:400]}"
            )

        # ASTAP writes a .wcs sidecar; parse it
        wcs_path = fits_path.with_suffix(".wcs")
        ini_path = fits_path.with_suffix(".ini")
        result = self._parse_ini(ini_path) if ini_path.exists() else {}
        if wcs_path.exists():
            result["wcs_text"] = wcs_path.read_text(encoding="utf-8")
        if not result:
            raise AstapError("ASTAP produced no sidecar — solve likely failed")
        return result

    def _parse_ini(self, ini_path: Path) -> dict:
        out: dict[str, Any] = {}
        for line in ini_path.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
        return out
