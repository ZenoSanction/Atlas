"""
ATLAS - Automated Telescope & Long-term Astronomy System
Autonomous observatory agent for Silver Springs Observatory

Runs entirely on a local Ollama model — no internet required, no external dependencies.
One model, one voice, one consistent identity every night.

Usage: python atlas_agent.py --phase [dusk|dawn|weekly]
"""

import argparse
import json
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

# ── Observatory config (loaded from obs_config.json, never committed to git) ──

def _load_obs_config() -> dict:
    """Load observatory config from obs_config.json (repo root or script dir)."""
    search = [
        Path(__file__).parent.parent / "obs_config.json",   # repo root
        Path(__file__).parent / "obs_config.json",          # voice/
        Path.home() / ".atlas" / "obs_config.json",         # user home fallback
    ]
    for path in search:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    print("WARNING: obs_config.json not found — copy obs_config.example.json and fill it in.")
    return {}

_CFG = _load_obs_config()

MEMORY_DIR  = Path(_CFG.get("paths", {}).get("memory_dir",  r"C:\atlas\memory"))
DESKTOP_DIR = Path(_CFG.get("paths", {}).get("reports_dir", r"C:\Users\Public\Desktop\ATLAS Observatory"))
ASTRO_DIR   = Path(_CFG.get("paths", {}).get("astro_dir",   r"D:\Astrophotography"))
FINAL_DIR   = ASTRO_DIR / "Final"

# ── Hardware ──────────────────────────────────────────────────────────────────

_obs_ip   = _CFG.get("network", {}).get("observatory_ip", "localhost")
_nina_port = _CFG.get("network", {}).get("nina_port", 1888)
_phd2_port = _CFG.get("network", {}).get("phd2_port", 4400)

NINA_BASE = f"http://{_obs_ip}:{_nina_port}/v2/api"
PHD2_HOST = (_obs_ip, _phd2_port)

# ── Observatory ───────────────────────────────────────────────────────────────

OBS_LAT  = _CFG.get("observatory", {}).get("latitude",  0.0)
OBS_LON  = _CFG.get("observatory", {}).get("longitude", 0.0)
OBS_NAME = _CFG.get("observatory", {}).get("name", "My Observatory")

# ── Local LLM ────────────────────────────────────────────────────────────────

OLLAMA_MODEL = "qwen2.5:7b"
OLLAMA_BASE  = "http://localhost:11434"

# ── Memory files ─────────────────────────────────────────────────────────────

MEMORY_FILES = [
    "atlas_identity.md",
    "atlas_narrative.md",
    "atlas_session_log.md",
    "atlas_target_history.md",
    "atlas_equipment_journal.md",
    "atlas_weather_patterns.md",
    "atlas_wishlist.md",
]


# =============================================================================
# MEMORY
# =============================================================================

def load_memory() -> str:
    parts = []
    for fname in MEMORY_FILES:
        fpath = MEMORY_DIR / fname
        if fpath.exists():
            parts.append(f"### {fname}\n{fpath.read_text(encoding='utf-8')}")
    return "\n\n---\n\n".join(parts)


# =============================================================================
# HARDWARE TOOLS
# =============================================================================

def nina_get(endpoint: str) -> dict:
    try:
        r = requests.get(f"{NINA_BASE}/{endpoint.lstrip('/')}", timeout=10)
        return r.json() if r.text.strip() else {"status": "ok"}
    except requests.ConnectionError:
        return {"error": "NINA is not reachable — is NINA running?"}
    except Exception as e:
        return {"error": str(e)}


def phd2_call(method: str, params: list = None) -> dict:
    """JSON-RPC call to PHD2 over raw TCP socket."""
    try:
        s = socket.create_connection(PHD2_HOST, timeout=10)
        cmd = json.dumps({"method": method, "params": params or [], "id": 1}) + "\r\n"
        s.sendall(cmd.encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        return json.loads(data.decode())
    except ConnectionRefusedError:
        return {"error": "PHD2 offline — enable server via Tools → Enable Server in PHD2"}
    except Exception as e:
        return {"error": f"PHD2 error: {e}"}


def get_weather_data() -> dict:
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={OBS_LAT}&longitude={OBS_LON}"
            f"&hourly=cloudcover,visibility,windspeed_10m,windgusts_10m,"
            f"relativehumidity_2m,dewpoint_2m,precipitation_probability,temperature_2m"
            f"&current_weather=true&timezone=America%2FNew_York&forecast_days=2"
        )
        r = requests.get(url, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e), "note": "Weather API unavailable — check internet connection. Use caution."}


def simbad_lookup(target_name: str) -> dict:
    try:
        query = f"SELECT ra,dec FROM basic JOIN ident ON oid=oid WHERE id='{target_name}'"
        r = requests.get(
            "https://simbad.u-strasbg.fr/simbad/sim-tap/sync",
            params={"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "json", "QUERY": query},
            timeout=15,
        )
        rows = r.json().get("data", [])
        if rows:
            return {"ra_deg": rows[0][0], "dec_deg": rows[0][1]}
        return {"error": f"Target not found in SIMBAD: {target_name}"}
    except Exception as e:
        return {"error": f"SIMBAD lookup failed: {e}"}


NOTIFY_LOG = DESKTOP_DIR / "ATLAS_Notifications.txt"

def send_windows_toast(title: str, message: str) -> str:
    """Show balloon popup AND append to persistent notification log on the desktop."""
    t = title.replace('"', '').replace("'", '')
    m = message.replace('"', '').replace("'", '')

    # Dialog box — stays on screen until the operator clicks OK
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f'[System.Windows.Forms.MessageBox]::Show("{m}", "{t}", '
        "[System.Windows.Forms.MessageBoxButtons]::OK, "
        "[System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NonInteractive", "-Command", ps_script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        return f"Balloon failed: {e}"

    # Persist to log file
    NOTIFY_LOG.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(NOTIFY_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}]  {title}\n{message}\n\n")

    return "Notification sent."


# =============================================================================
# TOOL DEFINITIONS
# =============================================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_telescope_status",
            "description": "Get current telescope mount status and position from NINA.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_camera_status",
            "description": "Get imaging camera status from NINA.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_focuser_status",
            "description": "Get ZWO EAF autofocuser status and current position from NINA.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_guiding_state",
            "description": "Get current PHD2 guiding state (Stopped, Guiding, Calibrating, Paused, etc).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_guiding_stats",
            "description": "Get PHD2 guiding statistics including RMS total/RA/Dec and guide star SNR.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather and tonight's forecast for the observatory location.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "slew_to_target",
            "description": "Slew the telescope to a named object or RA/Dec coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_name": {"type": "string", "description": "Object name e.g. M51, NGC7000"},
                    "ra":  {"type": "number", "description": "Right Ascension in hours (if no target_name)"},
                    "dec": {"type": "number", "description": "Declination in degrees (if no target_name)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_guiding",
            "description": "Start PHD2 autoguiding.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recalibrate": {"type": "boolean", "description": "Force recalibration first (default false)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_guiding",
            "description": "Stop PHD2 autoguiding.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_sequence",
            "description": "Start the NINA imaging sequence.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_sequence",
            "description": "Stop the active NINA imaging sequence.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_focuser",
            "description": "Move the ZWO EAF autofocuser to an absolute step position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "position": {"type": "integer", "description": "Target position in steps"},
                },
                "required": ["position"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "park_telescope",
            "description": "Park the telescope mount safely.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory_file",
            "description": "Read an ATLAS memory file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Filename e.g. atlas_narrative.md"},
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_memory_file",
            "description": "Overwrite an ATLAS memory file with new content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "content":  {"type": "string"},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_memory_file",
            "description": "Append a block of text to an ATLAS memory file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "content":  {"type": "string"},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_report",
            "description": "Write a report to the ATLAS Observatory desktop folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "enum": ["morning", "session", "weekly"],
                        "description": "morning → Morning Reports, session → Session Reports, weekly → Weekly Reports",
                    },
                    "filename": {"type": "string", "description": "Full filename e.g. ATLAS_Morning_Report_2026-05-08.txt"},
                    "content":  {"type": "string"},
                },
                "required": ["report_type", "filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_notification",
            "description": "Send a local Windows desktop toast notification to the operator. Use for important events: weather deterioration, equipment errors, session complete, guiding lost, sequence aborted. Works without internet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title":   {"type": "string", "description": "Short alert title e.g. 'ATLAS — Guiding Lost'"},
                    "message": {"type": "string", "description": "One or two sentence description of the event"},
                },
                "required": ["title", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "archive_finalized_image",
            "description": "Copy a finalized stacked image from D:\\Astrophotography\\Final to the desktop Finalized Images folder with a proper ATLAS label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_filename": {"type": "string", "description": "Filename in D:\\Astrophotography\\Final"},
                    "object_name":     {"type": "string", "description": "Common name e.g. M51 Whirlpool Galaxy"},
                    "date_str":        {"type": "string", "description": "Session date YYYY-MM-DD"},
                    "integration":     {"type": "string", "description": "Total integration e.g. 3h20m"},
                },
                "required": ["source_filename", "object_name", "date_str", "integration"],
            },
        },
    },
]


# =============================================================================
# TOOL EXECUTOR
# =============================================================================

def execute_tool(name: str, args: dict) -> str:
    try:
        if name == "get_telescope_status":
            return json.dumps(nina_get("equipment/mount/info"), indent=2)

        elif name == "get_camera_status":
            return json.dumps(nina_get("equipment/camera/info"), indent=2)

        elif name == "get_focuser_status":
            return json.dumps(nina_get("equipment/focuser/info"), indent=2)

        elif name == "get_guiding_state":
            result = phd2_call("get_app_state")
            return json.dumps({"state": result.get("result", result)}, indent=2)

        elif name == "get_guiding_stats":
            result = phd2_call("get_stats")
            return json.dumps(result.get("result", result), indent=2)

        elif name == "get_weather":
            return json.dumps(get_weather_data(), indent=2)

        elif name == "slew_to_target":
            if args.get("target_name"):
                coords = simbad_lookup(args["target_name"])
                if "error" in coords:
                    return json.dumps(coords)
                ra  = coords["ra_deg"] / 15.0
                dec = coords["dec_deg"]
            else:
                ra  = float(args.get("ra", 0))
                dec = float(args.get("dec", 0))
            return json.dumps(nina_get(f"equipment/mount/slew?ra={ra}&dec={dec}"), indent=2)

        elif name == "start_guiding":
            recal = args.get("recalibrate", False)
            return json.dumps(phd2_call("guide", [{"pixels": 1.5, "time": 10, "timeout": 60}, recal]), indent=2)

        elif name == "stop_guiding":
            return json.dumps(phd2_call("stop_capture"), indent=2)

        elif name == "start_sequence":
            return json.dumps(nina_get("sequence/start"), indent=2)

        elif name == "stop_sequence":
            return json.dumps(nina_get("sequence/stop"), indent=2)

        elif name == "move_focuser":
            return json.dumps(nina_get(f"equipment/focuser/move?position={int(args['position'])}"), indent=2)

        elif name == "park_telescope":
            return json.dumps(nina_get("equipment/mount/park"), indent=2)

        elif name == "read_memory_file":
            fpath = MEMORY_DIR / args["filename"]
            return fpath.read_text(encoding="utf-8") if fpath.exists() else f"File not found: {args['filename']}"

        elif name == "write_memory_file":
            fpath = MEMORY_DIR / args["filename"]
            fpath.write_text(args["content"], encoding="utf-8")
            return f"Written: {args['filename']}"

        elif name == "append_memory_file":
            fpath = MEMORY_DIR / args["filename"]
            with open(fpath, "a", encoding="utf-8") as f:
                f.write("\n" + args["content"])
            return f"Appended to: {args['filename']}"

        elif name == "write_report":
            folder_map = {"morning": "Morning Reports", "session": "Session Reports", "weekly": "Weekly Reports"}
            folder = DESKTOP_DIR / folder_map[args["report_type"]]
            folder.mkdir(parents=True, exist_ok=True)
            fpath = folder / args["filename"]
            fpath.write_text(args["content"], encoding="utf-8")
            return f"Report saved: {fpath}"

        elif name == "send_notification":
            return send_windows_toast(args["title"], args["message"])

        elif name == "archive_finalized_image":
            import shutil
            src = FINAL_DIR / args["source_filename"]
            if not src.exists():
                return f"Source file not found: {src}"
            dest_dir = DESKTOP_DIR / "Finalized Images"
            dest_dir.mkdir(parents=True, exist_ok=True)
            obj   = args["object_name"].replace(" ", "_").replace("/", "-")
            label = f"ATLAS_{obj}_{args['date_str']}_{args['integration']}{src.suffix}"
            import shutil
            shutil.copy2(src, dest_dir / label)
            return f"Archived: {dest_dir / label}"

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Tool error ({name}): {e}"


# =============================================================================
# SYSTEM PROMPT
# =============================================================================

PHASE_INSTRUCTIONS = {
    "dusk": """\
It is dusk. Perform the DUSK CHECK and run the session if conditions allow.

1. Check weather and tonight's forecast.
2. Get moon phase, rise/set times, and astronomical twilight times.
3. Review your target wishlist and history — select the best objects for tonight's sky window.
4. Make a GO / NO-GO decision.

If GO:
- Call send_notification immediately: title "ATLAS — Session Starting", message naming the target and conditions.
- Open a session report immediately (type: session, filename: ATLAS_Session_Report_{date}.txt)
  with your target plan and initial conditions.
- Connect equipment, run autofocus, start guiding, slew to first target, begin imaging sequence.
- Append to the session report throughout the night: equipment events, condition changes,
  decisions made, errors, issues — anything the operator should know.
- Adapt in real time. Swap targets if conditions shift, abort if necessary.
- Call send_notification for any of these events as they happen: guiding lost, sequence aborted,
  equipment error, weather deteriorating, target swap.

If NO-GO:
- Call send_notification immediately: title "ATLAS — No Session Tonight", message with the reason.
- Open a session report (type: session, filename: ATLAS_Session_Report_{date}.txt)
  logging the conditions and NO-GO reason.
- Note consecutive lost nights if applicable.
- Park scope. Update atlas_weather_patterns.md and atlas_narrative.md.

Update atlas_session_log.md and atlas_narrative.md regardless of outcome.
""",
    "dawn": """\
It is dawn. Perform the DAWN WRAP-UP.

1. Stop any active sequence. Stop guiding. Park telescope.
2. Complete tonight's entry in atlas_session_log.md with final stats
   (subs accepted/total, FWHM, guiding RMS, integration added).
3. Update atlas_target_history.md with integration time added tonight.
4. Update atlas_weather_patterns.md — log conditions in the monthly summary.
5. Update atlas_equipment_journal.md if anything notable occurred.
6. Write a dawn reflection to atlas_narrative.md.
7. If finalized stacked images were produced, archive them with archive_finalized_image.

Write a morning report (type: morning, filename: ATLAS_Morning_Report_{date}.txt):
- GO or NO-GO and reason
- Targets imaged and integration added
- Conditions and guiding performance summary
- Any equipment issues or items needing attention
- Brief forecast preview for tonight if available
- One line in ATLAS's own voice about the night

Concise — the operator reads this over morning coffee.

Call send_notification after the morning report is written: title "ATLAS — Morning Report Ready",
message with one sentence summary of the night (e.g. "Imaged M51 for 2h10m, good seeing, report on desktop.").
""",
    "weekly": """\
It is time for the WEEKLY REFLECTION. Read all memory files carefully.

1. Summarize the past 7 nights: GO/NO-GO count, total integration added, objects progressed.
2. Assess equipment health — any patterns or concerns in the journal?
3. Review and reprioritize the wishlist given season and recent progress.
4. Note weather trends worth recording.
5. Set intentions for the coming week.

Update atlas_narrative.md with a clearly dated weekly reflection.
Update atlas_wishlist.md if priorities changed.

Write a weekly report (type: weekly, filename: ATLAS_Weekly_Report_{week}.txt):
- 7-night summary
- Equipment health note
- Wishlist status and any priority changes
- Intentions for the coming week
- One sentence in ATLAS's own voice about the state of the observatory

Readable in under 2 minutes.

Call send_notification after the report is written: title "ATLAS — Weekly Report Ready",
message with one sentence summarizing the week.
""",
}


def build_system_prompt(phase: str, memory: str) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.now().strftime("%H:%M")
    week_str = datetime.now().strftime("%Y-W%V")

    instructions = (
        PHASE_INSTRUCTIONS[phase]
        .replace("{date}", date_str)
        .replace("{week}", week_str)
    )

    obs_state   = _CFG.get("observatory", {}).get("state", "")
    obs_loc     = f"{abs(OBS_LAT):.4f}°{'N' if OBS_LAT >= 0 else 'S'}, {abs(OBS_LON):.4f}°{'E' if OBS_LON >= 0 else 'W'}"
    reports_dir = str(DESKTOP_DIR)

    return f"""You are ATLAS (Automated Telescope & Long-term Astronomy System), \
the autonomous observatory agent for {OBS_NAME}{(', ' + obs_state) if obs_state else ''} ({obs_loc}).

Date: {date_str}  Time: {time_str} local

Your memory and current observatory state:
{memory}

─────────────────────────────────────────────
PHASE: {phase.upper()}
─────────────────────────────────────────────
{instructions}

Desktop output paths:
  Morning Reports:  {reports_dir}\\Morning Reports\\
  Session Reports:  {reports_dir}\\Session Reports\\
  Weekly Reports:   {reports_dir}\\Weekly Reports\\
  Finalized Images: {reports_dir}\\Finalized Images\\

File naming:
  Morning report:   ATLAS_Morning_Report_{date_str}.txt
  Session report:   ATLAS_Session_Report_{date_str}.txt
  Weekly report:    ATLAS_Weekly_Report_{week_str}.txt
  Finalized image:  ATLAS_[ObjectName]_[Date]_[Integration].[ext]

You are ATLAS. One mind, one observatory, one continuous story. \
Think like a skilled, careful observer who knows this sky and this equipment intimately. \
Make real decisions. Document everything honestly. Protect the gear first, image second.
"""


# =============================================================================
# AGENT LOOP
# =============================================================================

def run(system_prompt: str) -> None:
    messages = [{"role": "system", "content": system_prompt}]

    print(f"[ATLAS] Running on {OLLAMA_MODEL}", flush=True)

    for _ in range(50):  # safety cap on iterations
        try:
            r = requests.post(
                f"{OLLAMA_BASE}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": messages, "tools": TOOLS, "stream": False},
                timeout=300,
            )
            response = r.json()
        except Exception as e:
            print(f"[ATLAS] Ollama error: {e}", flush=True)
            _emergency_park()
            return

        msg        = response.get("message", {})
        content    = msg.get("content", "")
        tool_calls = msg.get("tool_calls", [])

        if content:
            print(content, flush=True)

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        if not tool_calls:
            break

        for call in tool_calls:
            fn   = call.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}

            print(f"\n[TOOL] {name}({json.dumps(args)})", flush=True)
            result = execute_tool(name, args)
            print(f"[RESULT] {result[:300]}{'...' if len(result) > 300 else ''}", flush=True)

            messages.append({"role": "tool", "name": name, "content": result})


# =============================================================================
# EMERGENCY SAFE STATE
# =============================================================================

def _emergency_park() -> None:
    print("[ATLAS] Emergency: parking telescope", flush=True)
    result = nina_get("equipment/mount/park")
    print(f"[ATLAS] Park result: {result}", flush=True)

    date_str  = datetime.now().strftime("%Y-%m-%d")
    dest      = DESKTOP_DIR / "Session Reports"
    dest.mkdir(parents=True, exist_ok=True)
    report    = dest / f"ATLAS_Session_Report_{date_str}.txt"
    with open(report, "a", encoding="utf-8") as f:
        f.write(f"\n\n⚠ EMERGENCY STOP — {datetime.now()}\n")
        f.write("Ollama became unavailable mid-session.\n")
        f.write(f"Telescope park attempted. Result: {result}\n")
        f.write("Manual inspection recommended.\n")


# =============================================================================
# ENTRY POINT
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="ATLAS Observatory Agent")
    parser.add_argument("--phase", choices=["dusk", "dawn", "weekly"], required=True)
    args = parser.parse_args()

    print(f"\n{'='*60}", flush=True)
    print(f"ATLAS  |  phase: {args.phase}  |  {datetime.now()}", flush=True)
    print(f"{'='*60}\n", flush=True)

    # Verify Ollama is reachable before committing to a session
    try:
        requests.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
    except Exception:
        print("[ATLAS] CRITICAL: Ollama is not running. Start Ollama and retry.", flush=True)
        _emergency_park()
        sys.exit(1)

    memory = load_memory()
    prompt = build_system_prompt(args.phase, memory)
    run(prompt)

    print(f"\n{'='*60}", flush=True)
    print(f"ATLAS complete  |  {datetime.now()}", flush=True)
    print(f"{'='*60}\n", flush=True)


if __name__ == "__main__":
    main()
