"""
ATLAS Observatory Server
========================
Runs on the observatory PC alongside NINA and PHD2.
Exposes all observatory functions as a REST API over the local network.

Network:
  Observatory PC : 192.168.50.245  (this machine)
  Warm Room PC   : 192.168.50.172  (dashboard machine)

Endpoints are consumed by the ATLAS Dashboard (atlas_dashboard.exe).

Dependencies:
    pip install -r requirements.txt

Usage:
    python atlas_server.py
    (Listens on 0.0.0.0:5000 — leave running in background)
"""

import asyncio
import json
import math
import datetime
import logging
import re
import subprocess
import threading
import httpx
import ollama

from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename="atlas_server.log",
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger("atlas_server")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NINA_BASE   = "http://localhost:1888/v2/api"
PHD2_HOST   = ("localhost", 4400)
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5000

OBS_LAT    = 29.2274
OBS_LON    = -82.0604
OBS_ELEV_M = 20
OBS_NAME   = "Silver Springs Observatory"

METEO_BASE  = "https://api.open-meteo.com/v1"
SIMBAD_TAP  = "https://simbad.u-strasbg.fr/simbad/sim-tap/sync"
OLLAMA_MODEL = "qwen2.5:7b"

WATCHDOG_DEFAULTS = {
    "enabled":               False,
    "poll_interval_sec":     120,
    "cloud_cover_limit_pct": 60,
    "wind_speed_limit_mph":  22,
    "wind_gust_limit_mph":   31,
    "humidity_limit_pct":    90,
    "dew_spread_limit_f":    4.5,
    "precip_limit_in":       0.005,
    "auto_stop_sequence":    True,
    "auto_park_telescope":   False,
}

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_watchdog: dict = dict(WATCHDOG_DEFAULTS)
_watchdog_task: Optional[asyncio.Task] = None
_watchdog_alerts: list = []
_watchdog_lock = asyncio.Lock()
_atlas_status: dict = {
    "verdict": "UNKNOWN",
    "reason": "System initializing...",
    "last_updated": None,
}
_chat_history: list = []

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="ATLAS Observatory Server", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helpers — NINA
# ---------------------------------------------------------------------------
async def nina_get(path: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{NINA_BASE}{path}")
            if not r.content:
                return {"error": "empty response"}
            return r.json()
    except Exception as e:
        return {"error": str(e)}

async def nina_post(path: str, data: dict = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{NINA_BASE}{path}", json=data or {})
            if not r.content:
                return {"success": True}
            return r.json()
    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------------------
# Helpers — PHD2
# ---------------------------------------------------------------------------
async def phd2(method: str, params=None) -> dict:
    try:
        reader, writer = await asyncio.open_connection(*PHD2_HOST)
        payload = json.dumps({"method": method, "params": params or [], "id": 1}) + "\r\n"
        writer.write(payload.encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=5)
        writer.close()
        return json.loads(data.decode())
    except ConnectionRefusedError:
        return {"error": "PHD2 not running or server not enabled"}
    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------------------
# Helpers — Weather
# ---------------------------------------------------------------------------
async def fetch_weather() -> dict:
    params = {
        "latitude": OBS_LAT, "longitude": OBS_LON,
        "current": ["temperature_2m","relative_humidity_2m","dew_point_2m",
                    "precipitation","cloud_cover","wind_speed_10m",
                    "wind_gusts_10m","surface_pressure","visibility"],
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch", "timezone": "America/New_York",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{METEO_BASE}/forecast", params=params)
            return r.json()
    except Exception as e:
        return {"error": str(e)}

def weather_verdict(w: dict) -> tuple[str, str]:
    """Returns (GO/CAUTION/NO-GO, reason)"""
    c = w.get("current", {})
    cloud  = c.get("cloud_cover", 0)
    precip = c.get("precipitation", 0)
    humid  = c.get("relative_humidity_2m", 0)
    wind   = c.get("wind_speed_10m", 0)
    gusts  = c.get("wind_gusts_10m", 0)
    temp   = c.get("temperature_2m", 70)
    dew    = c.get("dew_point_2m", 60)
    spread = temp - dew

    reasons = []
    verdict = "GO"

    if precip > 0.005:
        verdict = "NO-GO"
        reasons.append(f"Precipitation {precip:.3f}\"")
    if gusts > 31:
        verdict = "NO-GO"
        reasons.append(f"Dangerous gusts {gusts:.0f} mph")
    if cloud > 80:
        verdict = "NO-GO" if verdict != "NO-GO" else verdict
        reasons.append(f"Heavy cloud cover {cloud:.0f}%")
    if wind > 22 and verdict != "NO-GO":
        verdict = "CAUTION"
        reasons.append(f"Wind {wind:.0f} mph")
    if cloud > 50 and verdict == "GO":
        verdict = "CAUTION"
        reasons.append(f"Cloud cover {cloud:.0f}%")
    if humid > 90 and verdict == "GO":
        verdict = "CAUTION"
        reasons.append(f"Humidity {humid:.0f}%")
    if spread < 4.5 and verdict == "GO":
        verdict = "CAUTION"
        reasons.append(f"Dew spread {spread:.1f}°F — dew risk")

    if not reasons:
        reasons.append(f"Cloud {cloud:.0f}%, wind {wind:.0f} mph, humidity {humid:.0f}%")

    return verdict, ". ".join(reasons)

# ---------------------------------------------------------------------------
# Helpers — ATLAS AI assessment
# ---------------------------------------------------------------------------
async def atlas_assess() -> dict:
    """Ask ATLAS (Ollama) to assess overall observatory status."""
    try:
        weather_data = await fetch_weather()
        telescope    = await nina_get("/equipment/telescope")
        camera       = await nina_get("/equipment/camera")
        guiding      = await phd2("get_app_state")

        weather_verdict_str, weather_reason = weather_verdict(weather_data)
        c = weather_data.get("current", {})
        temp   = c.get("temperature_2m", "N/A")
        humid  = c.get("relative_humidity_2m", "N/A")
        dew    = c.get("dew_point_2m", "N/A")
        cloud  = c.get("cloud_cover", "N/A")
        wind   = c.get("wind_speed_10m", "N/A")
        gusts  = c.get("wind_gusts_10m", "N/A")
        precip = c.get("precipitation", "N/A")

        context = f"""You are ATLAS, the autonomous observatory agent at {OBS_NAME}.
All data below has already been retrieved for you. You do NOT need internet access.
Assess the current observatory status and provide a verdict.

LIVE WEATHER DATA (already fetched):
  Temperature : {temp}°F
  Humidity    : {humid}%
  Dew Point   : {dew}°F
  Cloud Cover : {cloud}%
  Wind Speed  : {wind} mph
  Wind Gusts  : {gusts} mph
  Precipitation: {precip}"
  Weather verdict: {weather_verdict_str} — {weather_reason}

EQUIPMENT STATUS:
  Telescope connected: {telescope.get('Connected', 'unknown')}
  Camera connected   : {camera.get('Connected', 'unknown')}
  PHD2 state         : {guiding.get('result', 'unknown')}

Respond with JSON only, in this exact format:
{{"verdict": "GO|CAUTION|NO-GO", "reason": "one plain-English sentence summarizing conditions and equipment"}}"""

        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": context}],
        )
        raw  = response["message"]["content"]
        text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        # Extract JSON from response
        start = text.find("{")
        end   = text.rfind("}") + 1
        result = json.loads(text[start:end])
        result["last_updated"] = datetime.datetime.now().isoformat()
        return result
    except Exception as e:
        log.error(f"atlas_assess error: {e}")
        return {
            "verdict": "UNKNOWN",
            "reason": f"ATLAS assessment unavailable: {e}",
            "last_updated": datetime.datetime.now().isoformat(),
        }

# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
async def status_refresh_loop():
    """Refresh ATLAS status verdict every 2 minutes."""
    global _atlas_status
    while True:
        _atlas_status = await atlas_assess()
        log.info(f"ATLAS status: {_atlas_status['verdict']} — {_atlas_status['reason']}")
        await asyncio.sleep(120)

async def watchdog_loop():
    """Safety watchdog — polls weather and stops sequence if limits breached."""
    global _watchdog_alerts
    while _watchdog.get("enabled"):
        weather_data = await fetch_weather()
        verdict, reason = weather_verdict(weather_data)
        if verdict == "NO-GO":
            async with _watchdog_lock:
                alert = {
                    "time": datetime.datetime.now().isoformat(),
                    "reason": reason,
                }
                _watchdog_alerts.append(alert)
                _watchdog_alerts[:] = _watchdog_alerts[-50:]
            if _watchdog.get("auto_stop_sequence"):
                await nina_post("/sequence/stop")
            if _watchdog.get("auto_park_telescope"):
                await nina_post("/equipment/telescope/park")
        await asyncio.sleep(_watchdog.get("poll_interval_sec", 120))

@app.on_event("startup")
async def startup():
    asyncio.create_task(status_refresh_loop())

# ===========================================================================
# API ENDPOINTS
# ===========================================================================

# ── Status ──────────────────────────────────────────────────────────────────

@app.get("/status")
async def get_status():
    """ATLAS overall verdict — GO/CAUTION/NO-GO with reason."""
    return _atlas_status

@app.post("/status/refresh")
async def refresh_status():
    """Force an immediate ATLAS status reassessment."""
    global _atlas_status
    _atlas_status = await atlas_assess()
    return _atlas_status

# ── Telescope ────────────────────────────────────────────────────────────────

@app.get("/telescope")
async def get_telescope():
    return await nina_get("/equipment/telescope")

@app.post("/telescope/slew")
async def slew_telescope(target_name: str = None, ra: float = None, dec: float = None):
    if target_name:
        return await nina_post("/equipment/telescope/slew", {"TargetName": target_name})
    elif ra is not None and dec is not None:
        return await nina_post("/equipment/telescope/slew", {"Ra": ra, "Dec": dec})
    raise HTTPException(400, "Provide target_name or ra/dec")

@app.post("/telescope/park")
async def park_telescope():
    return await nina_post("/equipment/telescope/park")

@app.post("/telescope/unpark")
async def unpark_telescope():
    return await nina_post("/equipment/telescope/unpark")

# ── Camera ───────────────────────────────────────────────────────────────────

@app.get("/camera")
async def get_camera():
    return await nina_get("/equipment/camera")

# ── Focuser ──────────────────────────────────────────────────────────────────

@app.get("/focuser")
async def get_focuser():
    return await nina_get("/equipment/focuser")

@app.post("/focuser/move")
async def move_focuser(position: int):
    return await nina_get(f"/equipment/focuser/move?position={position}")

# ── Guiding ──────────────────────────────────────────────────────────────────

@app.get("/guiding/state")
async def get_guiding_state():
    return await phd2("get_app_state")

@app.get("/guiding/stats")
async def get_guiding_stats():
    return await phd2("get_stats")

@app.post("/guiding/start")
async def start_guiding(recalibrate: bool = False):
    return await phd2("guide", [{"pixels": 1.5, "time": 10, "timeout": 60}, recalibrate])

@app.post("/guiding/stop")
async def stop_guiding():
    return await phd2("stop_capture")

# ── Sequence ─────────────────────────────────────────────────────────────────

@app.post("/sequence/start")
async def start_sequence():
    return await nina_post("/sequence/start")

@app.post("/sequence/stop")
async def stop_sequence():
    return await nina_post("/sequence/stop")

@app.get("/sequence/status")
async def get_sequence_status():
    return await nina_get("/sequence")

# ── Weather ──────────────────────────────────────────────────────────────────

@app.get("/weather")
async def get_weather():
    data = await fetch_weather()
    verdict, reason = weather_verdict(data)
    c = data.get("current", {})
    return {
        "verdict": verdict,
        "reason": reason,
        "temperature_f": c.get("temperature_2m"),
        "humidity_pct": c.get("relative_humidity_2m"),
        "dew_point_f": c.get("dew_point_2m"),
        "cloud_cover_pct": c.get("cloud_cover"),
        "precipitation_in": c.get("precipitation"),
        "wind_speed_mph": c.get("wind_speed_10m"),
        "wind_gusts_mph": c.get("wind_gusts_10m"),
        "pressure_hpa": c.get("surface_pressure"),
        "visibility_m": c.get("visibility"),
    }

@app.get("/weather/forecast")
async def get_forecast(hours: int = 12):
    params = {
        "latitude": OBS_LAT, "longitude": OBS_LON,
        "hourly": ["temperature_2m","relative_humidity_2m","dew_point_2m",
                   "precipitation_probability","cloud_cover","wind_speed_10m",
                   "wind_gusts_10m"],
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch", "timezone": "America/New_York",
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{METEO_BASE}/forecast", params=params)
            data = r.json()
        hourly = data.get("hourly", {})
        times  = hourly.get("time", [])[:hours]
        result = []
        for i, t in enumerate(times):
            cloud  = hourly.get("cloud_cover", [0]*24)[i]
            precip = hourly.get("precipitation_probability", [0]*24)[i]
            wind   = hourly.get("wind_speed_10m", [0]*24)[i]
            gusts  = hourly.get("wind_gusts_10m", [0]*24)[i]
            humid  = hourly.get("relative_humidity_2m", [0]*24)[i]
            temp   = hourly.get("temperature_2m", [70]*24)[i]
            dew    = hourly.get("dew_point_2m", [60]*24)[i]
            spread = temp - dew
            if precip > 30 or cloud > 80 or gusts > 31:
                v = "NO-GO"
            elif cloud > 50 or wind > 22 or humid > 90 or spread < 4.5:
                v = "CAUTION"
            else:
                v = "GO"
            result.append({
                "time": t, "verdict": v,
                "cloud_cover_pct": cloud,
                "precip_probability_pct": precip,
                "wind_speed_mph": wind,
                "wind_gusts_mph": gusts,
                "humidity_pct": humid,
                "dew_spread_f": round(spread, 1),
            })
        return result
    except Exception as e:
        return {"error": str(e)}

# ── Moon & Twilight ───────────────────────────────────────────────────────────

@app.get("/moon")
async def get_moon():
    now = datetime.datetime.now()
    # Approximate moon phase calculation
    known_new = datetime.datetime(2000, 1, 6, 18, 14)
    cycle = 29.53058867
    days_since = (now - known_new).total_seconds() / 86400
    phase_day  = days_since % cycle
    illumination = (1 - math.cos(2 * math.pi * phase_day / cycle)) / 2 * 100
    if phase_day < 1:       phase_name = "New Moon"
    elif phase_day < 7.4:   phase_name = "Waxing Crescent"
    elif phase_day < 8.4:   phase_name = "First Quarter"
    elif phase_day < 14.8:  phase_name = "Waxing Gibbous"
    elif phase_day < 15.8:  phase_name = "Full Moon"
    elif phase_day < 22.1:  phase_name = "Waning Gibbous"
    elif phase_day < 23.1:  phase_name = "Last Quarter"
    else:                   phase_name = "Waning Crescent"
    return {
        "phase_name": phase_name,
        "illumination_pct": round(illumination, 1),
        "phase_day": round(phase_day, 1),
    }

@app.get("/twilight")
async def get_twilight():
    return await nina_get("/utility/twilight")

# ── Watchdog ─────────────────────────────────────────────────────────────────

@app.get("/watchdog")
async def get_watchdog():
    async with _watchdog_lock:
        return {**_watchdog, "alerts": list(_watchdog_alerts)}

@app.post("/watchdog/start")
async def start_watchdog(config: dict = None):
    global _watchdog_task
    if config:
        _watchdog.update(config)
    _watchdog["enabled"] = True
    if _watchdog_task and not _watchdog_task.done():
        _watchdog_task.cancel()
    _watchdog_task = asyncio.create_task(watchdog_loop())
    return {"status": "watchdog started", "config": _watchdog}

@app.post("/watchdog/stop")
async def stop_watchdog():
    global _watchdog_task
    _watchdog["enabled"] = False
    if _watchdog_task:
        _watchdog_task.cancel()
    return {"status": "watchdog stopped"}

@app.post("/watchdog/thresholds")
async def set_watchdog_thresholds(thresholds: dict):
    _watchdog.update(thresholds)
    return {"status": "updated", "config": _watchdog}

# ── ATLAS Chat ───────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str

@app.post("/atlas/chat")
async def atlas_chat(req: ChatRequest):
    """Stream a response from ATLAS via Ollama — tokens arrive as they're generated."""
    try:
        weather_data = await fetch_weather()
        telescope    = await nina_get("/equipment/telescope")
        guiding      = await phd2("get_app_state")
        verdict, reason = weather_verdict(weather_data)

        c = weather_data.get("current", {})
        temp   = c.get("temperature_2m", "N/A")
        humid  = c.get("relative_humidity_2m", "N/A")
        dew    = c.get("dew_point_2m", "N/A")
        cloud  = c.get("cloud_cover", "N/A")
        wind   = c.get("wind_speed_10m", "N/A")
        gusts  = c.get("wind_gusts_10m", "N/A")
        precip = c.get("precipitation", "N/A")

        system_prompt = f"""You are ATLAS — Automated Telescope & Long-term Astronomy System.
You are the autonomous agent running {OBS_NAME}.
You are speaking with your operator from the warm room.
All data below has already been retrieved for you. You do NOT need internet access.

RESPONSE RULES — follow these exactly:
- Reply immediately and directly. No preamble, no reasoning, no "let me think".
- 1-3 sentences maximum unless a longer answer is truly needed.
- Never start with "I", "Sure", "Of course", "Certainly", or filler phrases.
- Do not explain your reasoning. Just give the answer.

LIVE WEATHER DATA (already fetched):
  Temperature : {temp}°F
  Humidity    : {humid}%
  Dew Point   : {dew}°F
  Cloud Cover : {cloud}%
  Wind Speed  : {wind} mph
  Wind Gusts  : {gusts} mph
  Precipitation: {precip}"
  Observing verdict: {verdict} — {reason}

EQUIPMENT STATUS:
  Telescope connected: {telescope.get('Connected', 'unknown')}
  PHD2 state         : {guiding.get('result', 'unknown')}"""

        _chat_history.append({"role": "user", "content": req.message})
        messages = [{"role": "system", "content": system_prompt}] + _chat_history[-10:]

        response = ollama.chat(model=OLLAMA_MODEL, messages=messages)
        raw   = response["message"]["content"]
        clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        reply = clean if clean else raw  # fallback if stripping leaves nothing

        _chat_history.append({"role": "assistant", "content": reply})
        _chat_history[:] = _chat_history[-20:]

        return {"reply": reply}

    except Exception as e:
        log.error(f"atlas_chat error: {e}")
        return {"reply": f"ATLAS unavailable: {e}"}

@app.delete("/atlas/chat/history")
async def clear_chat_history():
    _chat_history.clear()
    return {"status": "history cleared"}

# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "online",
        "observatory": OBS_NAME,
        "time": datetime.datetime.now().isoformat(),
    }

# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == "__main__":
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
