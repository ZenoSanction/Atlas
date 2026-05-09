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
    (includes: fastapi uvicorn httpx anthropic)

Usage:
    python atlas_server.py
    (Prompts for Anthropic API key on first run, then listens on 0.0.0.0:5000)
"""

import asyncio
import json
import math
import datetime
import logging
import os
import re
import subprocess
import sys
import threading
import httpx
import anthropic

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

OBS_LAT    = _OBS_CFG.get("observatory", {}).get("latitude",    29.2274)
OBS_LON    = _OBS_CFG.get("observatory", {}).get("longitude",  -82.0604)
OBS_ELEV_M = _OBS_CFG.get("observatory", {}).get("elevation_m", 20)
OBS_NAME   = _OBS_CFG.get("observatory", {}).get("name", "My Observatory")

METEO_BASE      = "https://api.open-meteo.com/v1"
SIMBAD_TAP      = "https://simbad.u-strasbg.fr/simbad/sim-tap/sync"
ANTHROPIC_MODEL = "claude-opus-4-7"   # used for session plan generation
CHAT_MODEL      = "claude-haiku-4-5"  # fast model for live chat and status assess

_IMAGING_CAMERA = _OBS_CFG.get("equipment", {}).get("imaging_camera", "Imaging Camera")
_GUIDE_CAMERA   = _OBS_CFG.get("equipment", {}).get("guide_camera",   "Guide Camera")
_FOCUSER        = _OBS_CFG.get("equipment", {}).get("focuser",        "Focuser")

CONFIG_FILE    = Path.home() / ".atlas" / "config.json"
OBS_CONFIG_FILE = Path(__file__).parent.parent / "obs_config.json"

def _load_obs_config() -> dict:
    """Load observatory config from obs_config.json (repo root) or home fallback."""
    search = [
        OBS_CONFIG_FILE,
        Path(__file__).parent / "obs_config.json",
        Path.home() / ".atlas" / "obs_config.json",
    ]
    for path in search:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    log.warning("obs_config.json not found — copy obs_config.example.json and fill it in.")
    return {}

_OBS_CFG = _load_obs_config()

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
# API key setup — call before starting server
# ---------------------------------------------------------------------------
def get_api_key() -> str:
    """Return the Anthropic API key, prompting and saving on first run."""
    # 1. Environment variable already set
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key

    # 2. Saved config file
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            key = cfg.get("ANTHROPIC_API_KEY", "").strip()
            if key:
                os.environ["ANTHROPIC_API_KEY"] = key
                return key
        except Exception:
            pass

    # 3. Prompt the user
    print("\n" + "=" * 56)
    print("  ATLAS Observatory Server — First-Run Setup")
    print("=" * 56)
    print("\nNo Anthropic API key found.")
    print("Get yours at: https://console.anthropic.com/settings/keys\n")
    key = input("Enter your Anthropic API key: ").strip()
    if not key:
        print("ERROR: An API key is required to run ATLAS.")
        sys.exit(1)

    # Save for next time
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps({"ANTHROPIC_API_KEY": key}, indent=2),
        encoding="utf-8",
    )
    os.environ["ANTHROPIC_API_KEY"] = key
    print(f"API key saved to {CONFIG_FILE}\n")
    return key

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
_chat_lock = asyncio.Lock()
_anthropic_client: Optional[anthropic.AsyncAnthropic] = None
_http_client: Optional[httpx.AsyncClient] = None
_status_task: Optional[asyncio.Task] = None

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
        r = await _http_client.get(f"{NINA_BASE}{path}")
        if not r.content:
            return {"error": "empty response"}
        return r.json()
    except Exception as e:
        return {"error": str(e)}

async def nina_post(path: str, data: dict = None) -> dict:
    try:
        r = await _http_client.post(f"{NINA_BASE}{path}", json=data or {})
        if not r.content:
            return {"success": True}
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------------------
# Helpers — PHD2
# ---------------------------------------------------------------------------
async def phd2(method: str, params=None) -> dict:
    reader = writer = None
    try:
        reader, writer = await asyncio.open_connection(*PHD2_HOST)
        payload = json.dumps({"method": method, "params": params or [], "id": 1}) + "\r\n"
        writer.write(payload.encode())
        await writer.drain()
        # PHD2 sends unsolicited Version + AppState events on connect before
        # the JSON-RPC response. Read lines until we find the one with "result".
        for _ in range(10):
            data = await asyncio.wait_for(reader.readline(), timeout=5)
            decoded = json.loads(data.decode())
            if "result" in decoded or "error" in decoded:
                return decoded
        return {"error": "PHD2 response not found in event stream"}
    except ConnectionRefusedError:
        return {"error": "PHD2 not running or server not enabled"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

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
        r = await _http_client.get(f"{METEO_BASE}/forecast", params=params)
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
    """Ask ATLAS (Claude) to assess overall observatory status."""
    try:
        weather_data, telescope, camera, focuser, guiding = await asyncio.gather(
            fetch_weather(),
            nina_get("/equipment/mount/info"),
            nina_get("/equipment/camera/info"),
            nina_get("/equipment/focuser/info"),
            phd2("get_app_state"),
        )

        weather_verdict_str, weather_reason = weather_verdict(weather_data)
        c = weather_data.get("current", {})
        temp   = c.get("temperature_2m", "N/A")
        humid  = c.get("relative_humidity_2m", "N/A")
        dew    = c.get("dew_point_2m", "N/A")
        cloud  = c.get("cloud_cover", "N/A")
        wind   = c.get("wind_speed_10m", "N/A")
        gusts  = c.get("wind_gusts_10m", "N/A")
        precip = c.get("precipitation", "N/A")

        focuser_resp     = focuser.get('Response', {})
        focuser_conn     = focuser_resp.get('Connected', 'unknown')
        focuser_pos      = focuser_resp.get('Position', 'unknown')

        context = f"""You are ATLAS, the autonomous observatory agent at {OBS_NAME}.
All data below has already been retrieved for you.
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
  Telescope connected: {telescope.get('Response', {}).get('Connected', 'unknown')}
  Imaging camera ({_IMAGING_CAMERA}): {camera.get('Response', {}).get('Connected', 'unknown')}
  Focuser ({_FOCUSER}) connected   : {focuser_conn} | position: {focuser_pos}
  PHD2 state                    : {guiding.get('result', 'unknown')}

Respond with JSON only, in this exact format:
{{"verdict": "GO|CAUTION|NO-GO", "reason": "one plain-English sentence summarizing conditions and equipment"}}"""

        response = await _anthropic_client.messages.create(
            model=CHAT_MODEL,
            max_tokens=200,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": context}],
        )
        # Extract text from response content blocks
        text = next((b.text for b in response.content if b.type == "text"), "")
        # Parse JSON from response
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
                await nina_post("/equipment/mount/park")
        await asyncio.sleep(_watchdog.get("poll_interval_sec", 120))

@app.on_event("startup")
async def startup():
    global _anthropic_client, _http_client, _status_task
    _http_client = httpx.AsyncClient(timeout=10)
    _anthropic_client = anthropic.AsyncAnthropic()
    _status_task = asyncio.create_task(status_refresh_loop())

@app.on_event("shutdown")
async def shutdown():
    global _status_task, _http_client
    if _status_task and not _status_task.done():
        _status_task.cancel()
    if _http_client:
        await _http_client.aclose()

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
    return await nina_get("/equipment/mount/info")

@app.post("/telescope/slew")
async def slew_telescope(target_name: str = None, ra: float = None, dec: float = None):
    if target_name:
        return await nina_post("/equipment/mount/slew", {"TargetName": target_name})
    elif ra is not None and dec is not None:
        return await nina_post("/equipment/mount/slew", {"Ra": ra, "Dec": dec})
    raise HTTPException(400, "Provide target_name or ra/dec")

@app.post("/telescope/park")
async def park_telescope():
    return await nina_post("/equipment/mount/park")

@app.post("/telescope/unpark")
async def unpark_telescope():
    return await nina_post("/equipment/mount/unpark")

# ── Camera ───────────────────────────────────────────────────────────────────

@app.get("/camera")
async def get_camera():
    return await nina_get("/equipment/camera/info")

# ── Focuser ──────────────────────────────────────────────────────────────────

@app.get("/focuser")
async def get_focuser():
    return await nina_get("/equipment/focuser/info")

@app.post("/focuser/move")
async def move_focuser(position: int):
    return await nina_post("/equipment/focuser/move", {"Position": position})

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
        r = await _http_client.get(f"{METEO_BASE}/forecast", params=params)
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
    """Send a message to ATLAS (Claude) and receive a reply with live observatory context."""
    try:
        weather_data, telescope, camera, focuser, guiding = await asyncio.gather(
            fetch_weather(),
            nina_get("/equipment/mount/info"),
            nina_get("/equipment/camera/info"),
            nina_get("/equipment/focuser/info"),
            phd2("get_app_state"),
        )
        verdict, reason = weather_verdict(weather_data)

        c = weather_data.get("current", {})
        temp   = c.get("temperature_2m", "N/A")
        humid  = c.get("relative_humidity_2m", "N/A")
        dew    = c.get("dew_point_2m", "N/A")
        cloud  = c.get("cloud_cover", "N/A")
        wind   = c.get("wind_speed_10m", "N/A")
        gusts  = c.get("wind_gusts_10m", "N/A")
        precip = c.get("precipitation", "N/A")

        now_local = datetime.datetime.now()
        now_utc   = datetime.datetime.now(datetime.timezone.utc)

        system_prompt = f"""You are ATLAS — Automated Telescope & Long-term Astronomy System.
You are the autonomous agent running {OBS_NAME}.
You are speaking with your operator from the warm room.
All data below has already been retrieved for you. You do NOT need internet access.

RESPONSE RULES — follow these exactly:
- Reply immediately and directly. No preamble, no reasoning, no "let me think".
- 1-3 sentences maximum unless a longer answer is truly needed.
- Never start with "I", "Sure", "Of course", "Certainly", or filler phrases.
- Do not explain your reasoning. Just give the answer.

DATE & TIME:
  Local : {now_local.strftime("%A, %B %d, %Y  %I:%M %p")}
  UTC   : {now_utc.strftime("%Y-%m-%d %H:%M UTC")}

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
  Telescope connected           : {telescope.get('Response', {}).get('Connected', 'unknown')}
  Imaging camera ({_IMAGING_CAMERA}): {camera.get('Response', {}).get('Connected', 'unknown')}
  Focuser ({_FOCUSER}) connected   : {focuser.get('Response', {}).get('Connected', 'unknown')} | position: {focuser.get('Response', {}).get('Position', 'unknown')}
  Guide camera ({_GUIDE_CAMERA})   : connects via PHD2 — see PHD2 state below
  PHD2 state                    : {guiding.get('result', 'unknown')}

PHD2 STATE DEFINITIONS — use these exact meanings:
  Stopped     : PHD2 is idle, not looping, not guiding.
  Looping     : PHD2 is capturing frames but no guide star selected yet.
  Selected    : PHD2 is looping and a guide star has been selected — ready to calibrate or guide.
  Calibrating : PHD2 is running the calibration routine.
  Guiding     : PHD2 is actively autoguiding — normal imaging state.
  LostLock    : PHD2 lost the guide star and paused guiding.
  Paused      : Guiding temporarily paused.

CAMERA NOTE: The imaging camera (NINA) and guide camera (PHD2) are separate devices.
Never say the guide camera is disconnected based on NINA camera status."""

        async with _chat_lock:
            _chat_history.append({"role": "user", "content": req.message})
            messages_snapshot = list(_chat_history[-10:])

        response = await _anthropic_client.messages.create(
            model=CHAT_MODEL,
            max_tokens=500,
            thinking={"type": "disabled"},
            system=system_prompt,
            messages=messages_snapshot,
        )

        reply = next((b.text for b in response.content if b.type == "text"), "").strip()

        async with _chat_lock:
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

@app.post("/atlas/session-plan")
async def generate_session_plan():
    """Ask ATLAS (Claude) to generate a full nightly session plan."""
    try:
        # Gather all data in parallel
        weather_data, telescope, camera, focuser, moon_data = await asyncio.gather(
            fetch_weather(),
            nina_get("/equipment/mount/info"),
            nina_get("/equipment/camera/info"),
            nina_get("/equipment/focuser/info"),
            asyncio.to_thread(_get_moon_data),
        )

        # Hourly forecast for tonight
        forecast_params = {
            "latitude": OBS_LAT, "longitude": OBS_LON,
            "hourly": ["temperature_2m", "relative_humidity_2m", "dew_point_2m",
                       "precipitation_probability", "cloud_cover", "wind_speed_10m",
                       "wind_gusts_10m"],
            "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch", "timezone": "America/New_York",
            "forecast_days": 2,
        }
        try:
            r = await _http_client.get(f"{METEO_BASE}/forecast", params=forecast_params)
            forecast_raw = r.json()
        except Exception:
            forecast_raw = {}

        # Build tonight's imaging window forecast (21:00 – 05:00)
        hourly    = forecast_raw.get("hourly", {})
        times     = hourly.get("time", [])
        clouds    = hourly.get("cloud_cover", [])
        winds     = hourly.get("wind_speed_10m", [])
        gusts     = hourly.get("wind_gusts_10m", [])
        humids    = hourly.get("relative_humidity_2m", [])
        temps     = hourly.get("temperature_2m", [])
        dews      = hourly.get("dew_point_2m", [])
        precips   = hourly.get("precipitation_probability", [])

        now_local = datetime.datetime.now()
        today_str     = now_local.strftime("%Y-%m-%d")
        tomorrow_str  = (now_local + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        window_rows = []
        for i, t in enumerate(times):
            if i >= len(clouds):
                break
            hour = int(t[11:13])
            is_tonight   = t.startswith(today_str)    and hour >= 21
            is_tomorrow  = t.startswith(tomorrow_str) and hour <= 6
            if is_tonight or is_tomorrow:
                cloud = clouds[i] if i < len(clouds) else "?"
                wind  = winds[i]  if i < len(winds)  else "?"
                gust  = gusts[i]  if i < len(gusts)  else "?"
                humid = humids[i] if i < len(humids) else "?"
                temp  = temps[i]  if i < len(temps)  else "?"
                dew   = dews[i]   if i < len(dews)   else "?"
                precip = precips[i] if i < len(precips) else "?"
                spread = round(temp - dew, 1) if isinstance(temp, (int, float)) and isinstance(dew, (int, float)) else "?"
                if isinstance(cloud, (int,float)) and isinstance(gust, (int,float)) and isinstance(precip, (int,float)):
                    if cloud > 80 or gust > 31 or precip > 30:
                        verdict = "NO-GO"
                    elif cloud > 50 or wind > 22 or humid > 90 or (isinstance(spread, float) and spread < 4.5):
                        verdict = "CAUTION"
                    else:
                        verdict = "GO"
                else:
                    verdict = "?"
                # Convert to 12-hour AM/PM for display
                hour_int = int(t[11:13])
                minute   = t[14:16]
                period   = "AM" if hour_int < 12 else "PM"
                hour12   = hour_int % 12 or 12
                t12      = f"{hour12}:{minute}{period}"
                window_rows.append(
                    f"  {t12:<9} {verdict:<8} cloud={cloud}%  wind={wind}/{gust} mph  "
                    f"humid={humid}%  dew_spread={spread}°F"
                )

        forecast_block = "\n".join(window_rows) if window_rows else "  No forecast data available."

        # Current conditions
        c       = weather_data.get("current", {})
        verdict, reason = weather_verdict(weather_data)

        # Equipment
        tel_conn = telescope.get('Response', {}).get('Connected', False)
        cam_conn = camera.get('Response', {}).get('Connected', False)
        foc_conn = focuser.get('Response', {}).get('Connected', False)
        foc_pos  = focuser.get('Response', {}).get('Position', 'unknown')

        prompt = f"""You are ATLAS, the autonomous observatory agent for {OBS_NAME}.
Generate a complete nightly session plan for tonight based on the data below.
Write in plain text — no markdown, no asterisks, no bullet dashes. Use section headers in ALL CAPS.
Be specific, practical, and direct. Write as a knowledgeable observatory operator would.

DATE: {now_local.strftime("%A, %B %d, %Y")}
LOCAL TIME: {now_local.strftime("%I:%M %p")}
LOCATION: {OBS_NAME} — Lat {OBS_LAT}°N, Lon {OBS_LON}°W, Elev {OBS_ELEV_M}m

CURRENT CONDITIONS:
  Temperature : {c.get('temperature_2m', 'N/A')}°F
  Humidity    : {c.get('relative_humidity_2m', 'N/A')}%
  Dew Point   : {c.get('dew_point_2m', 'N/A')}°F
  Cloud Cover : {c.get('cloud_cover', 'N/A')}%
  Wind        : {c.get('wind_speed_10m', 'N/A')} mph (gusts {c.get('wind_gusts_10m', 'N/A')} mph)
  Precipitation: {c.get('precipitation', 'N/A')}"
  Overall Verdict: {verdict} — {reason}

TONIGHT'S IMAGING WINDOW FORECAST (21:00 tonight through 06:00 tomorrow):
{forecast_block}

MOON:
  Phase       : {moon_data.get('phase_name', 'unknown')}
  Illumination: {moon_data.get('illumination_pct', 'unknown')}%
  Phase Day   : {moon_data.get('phase_day', 'unknown')} of 29.5

EQUIPMENT STATUS:
  Mount    : {"Connected" if tel_conn else "Disconnected"}
  Imaging Camera (ASI 585MC Pro): {"Connected" if cam_conn else "Disconnected"}
  Focuser (ZWO EAF): {"Connected, position " + str(foc_pos) if foc_conn else "Disconnected"}
  Guide Camera (SVBony SV205): Connects via PHD2 separately

Write a session plan with these sections:
TONIGHT'S OUTLOOK — 2-3 sentences on overall conditions and whether imaging is viable.
IMAGING WINDOW — The best hours to image tonight and why.
MOON IMPACT — How the moon affects target selection tonight.
RECOMMENDED TARGETS — 3 to 5 specific deep sky objects well-suited to tonight's conditions, moon phase, and Florida skies. For each give the name, type, constellation, why it suits tonight, and suggested exposure strategy.
SUGGESTED SEQUENCE — A time-ordered schedule for the night.
EQUIPMENT NOTES — Current hardware status and anything to address before imaging.
CAUTIONS — Any weather, dew, or equipment risks to watch during the session."""

        response = await _anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1500,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )

        plan = next((b.text for b in response.content if b.type == "text"), "").strip()
        return {"plan": plan, "generated": now_local.isoformat()}

    except Exception as e:
        log.error(f"session_plan error: {e}")
        return {"plan": f"Session plan unavailable: {e}", "generated": None}


def _get_moon_data() -> dict:
    """Synchronous moon phase calculation (for use with asyncio.to_thread)."""
    now = datetime.datetime.now()
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
    get_api_key()
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
