"""PHD2 event-stream subscriber with rolling RMS buffer.

PHD2 doesn't have a "give me current RMS" RPC. Guiding statistics come
out as a continuous stream of JSON-RPC events on the same socket — one
``GuideStep`` event per guide exposure, each carrying:

    {"Event": "GuideStep",
     "Timestamp": <unix>,
     "RADistanceRaw": <pixels>,    signed (RA error this step)
     "DECDistanceRaw": <pixels>,   signed (DEC error this step)
     "ErrorCode": <int>,           0 == no error
     ...}

We open one long-lived connection to PHD2, swallow everything that comes
back, keep a rolling buffer of the last N GuideStep events, and compute
RMS-total / RMS-RA / RMS-DEC on demand. The Critic fast loop asks this
class for "what's the current guiding RMS?" instead of trying to RPC
PHD2 every 90 s — far more accurate (PHD2 itself computes RMS over a
sliding window, this matches its behaviour) and cheaper.

Sim mode: with no real PHD2 to talk to, the stream emits synthetic
GuideStep events at 2 s cadence with realistic Gaussian noise (σ ≈
0.4 pixels) so the dashboard's RMS readout is non-zero and the fast
loop's threshold logic can be exercised. The synthetic source is
swapped for the real socket the moment is_simulation_mode() flips off.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

from atlas.logging_setup import get_logger

log = get_logger("hardware.phd2_events")

# How many GuideStep samples to keep. At PHD2's typical 2 s cadence
# this is ~2 minutes of history — enough to smooth noise but short
# enough that recent excursions still pull the RMS up.
_BUFFER_DEPTH = 60
# Cadence of synthetic GuideStep events in sim mode (seconds).
_SIM_STEP_S = 2.0
# Typical "good guiding" Gaussian σ for synthetic events (pixels).
_SIM_SIGMA_PX = 0.4


@dataclass
class GuideStep:
    timestamp: float          # unix seconds
    ra_error_px: float        # signed
    dec_error_px: float       # signed
    error_code: int = 0       # PHD2's error flag (0 = no error)


@dataclass
class GuidingStats:
    """Snapshot of the current rolling RMS. Returned by `current()` —
    cheap to call (the buffer is held in memory)."""
    rms_total_px: float
    rms_ra_px: float
    rms_dec_px: float
    rms_total_arcsec: Optional[float]
    rms_ra_arcsec: Optional[float]
    rms_dec_arcsec: Optional[float]
    sample_count: int
    last_step_age_s: float
    star_lost_recent: bool
    app_state: str            # PHD2's app_state at last poll

    def to_jsonable(self) -> dict:
        return asdict(self)


def _rms(samples: list[float]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class Phd2EventStream:
    """One long-lived TCP connection to PHD2 + rolling event buffer.

    Owned by the Critic agent — instantiated in `Critic.run()` and torn
    down when the agent stops. The Critic fast loop just calls
    ``current(pixel_scale)`` to get the latest RMS.

    Real mode: connects to host:port, reads newline-delimited JSON, and
    files any GuideStep events into the buffer. Reconnects on disconnect
    with exponential backoff up to 60 s.

    Sim mode: spawns an asyncio task that synthesizes GuideStep events
    at _SIM_STEP_S cadence with Gaussian noise. Lets the Critic's
    guiding_lost / guiding_drift logic + the dashboard RMS readout run
    against believable values with no hardware connected.
    """

    def __init__(self, host: str = "localhost", port: int = 4400,
                 simulation: bool = False) -> None:
        self._host = host
        self._port = port
        self._simulation = simulation
        self._steps: deque[GuideStep] = deque(maxlen=_BUFFER_DEPTH)
        self._app_state: str = "Stopped"
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._reconnect_delay = 2.0
        # Track the time we last saw a StarLost event for the "star
        # lost recently" flag in GuidingStats.
        self._last_star_lost_at: float = 0.0

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self._run(),
                name=f"phd2-events-{'sim' if self._simulation else 'real'}",
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ---- public read API ---------------------------------------------------

    def current(self, pixel_scale_arcsec: float | None = None) -> GuidingStats:
        """Compute RMS over the current buffer. Returns zeros + age 999
        if no samples yet (e.g. cold boot, PHD2 not started)."""
        if not self._steps:
            return GuidingStats(
                rms_total_px=0.0, rms_ra_px=0.0, rms_dec_px=0.0,
                rms_total_arcsec=None, rms_ra_arcsec=None, rms_dec_arcsec=None,
                sample_count=0, last_step_age_s=999.0,
                star_lost_recent=False, app_state=self._app_state,
            )
        ra = [s.ra_error_px for s in self._steps]
        dec = [s.dec_error_px for s in self._steps]
        rms_ra = _rms(ra)
        rms_dec = _rms(dec)
        rms_total = math.sqrt(rms_ra ** 2 + rms_dec ** 2)
        age = time.time() - self._steps[-1].timestamp
        scale = pixel_scale_arcsec
        return GuidingStats(
            rms_total_px=round(rms_total, 3),
            rms_ra_px=round(rms_ra, 3),
            rms_dec_px=round(rms_dec, 3),
            rms_total_arcsec=(round(rms_total * scale, 3) if scale else None),
            rms_ra_arcsec=(round(rms_ra * scale, 3) if scale else None),
            rms_dec_arcsec=(round(rms_dec * scale, 3) if scale else None),
            sample_count=len(self._steps),
            last_step_age_s=round(age, 1),
            star_lost_recent=((time.time() - self._last_star_lost_at) < 30.0),
            app_state=self._app_state,
        )

    # ---- internals ---------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._simulation:
                    await self._run_simulation_once()
                else:
                    await self._run_real_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("phd2 event stream failed: %s — reconnecting "
                             "in %.0fs", e, self._reconnect_delay)
                try:
                    await asyncio.wait_for(self._stop.wait(),
                                              timeout=self._reconnect_delay)
                except asyncio.TimeoutError:
                    pass
                self._reconnect_delay = min(60.0, self._reconnect_delay * 1.7)

    async def _run_simulation_once(self) -> None:
        """Synthesize Gaussian-distributed RA/DEC errors at 2 s cadence.
        Models occasional brief excursions (5% chance per step of a
        2-3σ kick) so the alert thresholds get exercised over a long
        enough run."""
        log.info("Phd2EventStream running in simulation mode "
                  "(synthetic GuideStep at %.1fs)", _SIM_STEP_S)
        self._app_state = "Guiding"
        # Reset reconnect timer on successful "connection"
        self._reconnect_delay = 2.0
        while not self._stop.is_set():
            sigma = _SIM_SIGMA_PX
            ra_err = random.gauss(0, sigma)
            dec_err = random.gauss(0, sigma)
            # Occasional excursion
            if random.random() < 0.05:
                ra_err *= random.uniform(2.0, 3.0)
                dec_err *= random.uniform(2.0, 3.0)
            self._steps.append(GuideStep(timestamp=time.time(),
                                            ra_error_px=ra_err,
                                            dec_error_px=dec_err))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_SIM_STEP_S)
            except asyncio.TimeoutError:
                pass

    async def _run_real_once(self) -> None:
        """Real PHD2 connection: open TCP socket, read newline-delimited
        JSON events forever, file GuideStep events into the buffer."""
        log.info("Phd2EventStream connecting to %s:%d", self._host, self._port)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=5.0,
        )
        try:
            # Reset reconnect timer once we're actually reading data
            self._reconnect_delay = 2.0
            self._app_state = "Connected"
            while not self._stop.is_set():
                line = await reader.readline()
                if not line:
                    raise ConnectionError("PHD2 closed connection")
                try:
                    msg = json.loads(line.decode("utf-8").strip())
                except (ValueError, UnicodeDecodeError):
                    continue
                ev = msg.get("Event") or msg.get("event")
                if ev == "GuideStep":
                    self._steps.append(GuideStep(
                        timestamp=float(msg.get("Timestamp") or time.time()),
                        ra_error_px=float(msg.get("RADistanceRaw") or 0.0),
                        dec_error_px=float(msg.get("DECDistanceRaw") or 0.0),
                        error_code=int(msg.get("ErrorCode") or 0),
                    ))
                elif ev == "AppState":
                    self._app_state = str(msg.get("State", "")) or self._app_state
                elif ev == "StarLost":
                    self._last_star_lost_at = time.time()
                elif ev == "GuidingStopped":
                    self._app_state = "Stopped"
                elif ev == "StartGuiding":
                    self._app_state = "Guiding"
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


# ---- module-level singleton accessor (Critic owns one instance) ----------

_stream: Phd2EventStream | None = None


def get_event_stream(host: str = "localhost", port: int = 4400,
                       simulation: bool = False) -> Phd2EventStream:
    """Return the process-wide PHD2 event stream, creating it if needed."""
    global _stream
    if _stream is None:
        _stream = Phd2EventStream(host=host, port=port, simulation=simulation)
        _stream.start()
    return _stream


async def stop_event_stream() -> None:
    global _stream
    if _stream is not None:
        await _stream.stop()
        _stream = None
