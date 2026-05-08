"""
ATLAS Voice Interface — Continuous Listening Mode
Speak naturally. ATLAS detects when you start and stop talking, transcribes,
thinks, and responds aloud. No push-to-talk required.

Requirements:
    pip install faster-whisper sounddevice numpy pywin32 requests anthropic
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import anthropic
import numpy as np
import requests
import sounddevice as sd
import win32com.client
from faster_whisper import WhisperModel

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_MODEL    = "claude-opus-4-7"   # reserved for heavy tasks if needed
CHAT_MODEL         = "claude-haiku-4-5"  # fast model for live conversation
SAMPLE_RATE        = 16000
CHUNK_FRAMES       = 800          # 50ms chunks at 16kHz
SILENCE_DURATION   = 1.2          # seconds of quiet that ends an utterance
MIN_SPEECH_SECS    = 0.4          # ignore clips shorter than this
CALIBRATION_SECS   = 2.0          # seconds to measure ambient noise at startup
NOISE_MULTIPLIER   = 4.0          # threshold = ambient RMS × this factor

CONFIG_FILE = Path.home() / ".atlas" / "config.json"

MEMORY_DIR = Path(r"C:\Users\nasan\.claude\projects\C--Users-nasan\memory")
MEMORY_FILES = [
    "atlas_identity.md",
    "atlas_narrative.md",
    "atlas_session_log.md",
    "atlas_target_history.md",
    "atlas_equipment_journal.md",
    "atlas_weather_patterns.md",
    "atlas_wishlist.md",
]

# ── API key setup ─────────────────────────────────────────────────────────────

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
    print("  ATLAS Voice Interface — First-Run Setup")
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

# ── Memory ────────────────────────────────────────────────────────────────────

def load_memory() -> str:
    parts = []
    for fname in MEMORY_FILES:
        fpath = MEMORY_DIR / fname
        if fpath.exists():
            parts.append(f"### {fname}\n{fpath.read_text(encoding='utf-8')}")
    return "\n\n---\n\n".join(parts)

# ── TTS ───────────────────────────────────────────────────────────────────────

def _strip_markdown(text: str) -> str:
    """Remove markdown formatting so TTS reads clean natural speech."""
    text = re.sub(r"```[\s\S]*?```", "", text)       # code blocks
    text = re.sub(r"`[^`]*`", "", text)               # inline code
    text = re.sub(r"#{1,6}\s*", "", text)             # headings
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)     # bold
    text = re.sub(r"\*(.+?)\*", r"\1", text)          # italic
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)  # bullets
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)  # numbered lists
    text = re.sub(r"\n{2,}", " ", text)               # multiple newlines to space
    text = re.sub(r"\n", " ", text)                   # remaining newlines
    text = re.sub(r"\s{2,}", " ", text)               # multiple spaces
    return text.strip()


def speak(text: str) -> None:
    clean = _strip_markdown(text)
    print(f"\nATLAS: {clean}\n", flush=True)
    try:
        sapi = win32com.client.Dispatch("SAPI.SpVoice")
        sapi.Rate   = 0
        sapi.Volume = 100
        sapi.Speak(clean)
    except Exception as e:
        print(f"[TTS error] {e}", flush=True)

# ── Voice tools (Anthropic format) ───────────────────────────────────────────

VOICE_TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather conditions and tonight's forecast for Silver Springs Observatory.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_telescope_status",
        "description": "Get current telescope mount status from NINA.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_guiding_state",
        "description": "Get current PHD2 guiding state and statistics.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_camera_status",
        "description": "Get imaging camera status from NINA.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "send_notification",
        "description": (
            "Send a desktop notification to the operator. "
            "Use when asked to send a notification or test notifications, "
            "and for urgent alerts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title":   {"type": "string", "description": "Short notification title"},
                "message": {"type": "string", "description": "Notification body text"},
            },
            "required": ["title", "message"],
        },
    },
]

OBS_LAT = 29.2274
OBS_LON = -82.0604

GO_NO_GO_KEYWORDS = {"go", "no-go", "tonight", "session", "image", "imaging", "observe", "sky", "clear"}

def _evaluate_go_no_go() -> str:
    """Fetch weather and return a one-sentence GO/NO-GO verdict for tonight's imaging window."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={OBS_LAT}&longitude={OBS_LON}"
            f"&hourly=cloudcover,windspeed_10m,relativehumidity_2m,"
            f"precipitation_probability,temperature_2m,dewpoint_2m"
            f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
            f"&timezone=America%2FNew_York&forecast_days=2"
        )
        data = requests.get(url, timeout=10).json()
        times        = data["hourly"]["time"]
        clouds       = data["hourly"]["cloudcover"]
        wind         = data["hourly"]["windspeed_10m"]
        precip_prob  = data["hourly"]["precipitation_probability"]
        humidity     = data["hourly"]["relativehumidity_2m"]

        # Imaging window: 21:00 tonight through 05:00 tomorrow
        from datetime import date as _date, timedelta as _td
        tonight  = _date.today().strftime("%Y-%m-%d")
        tomorrow = (_date.today() + _td(days=1)).strftime("%Y-%m-%d")
        window = []
        for i, t in enumerate(times):
            hour = int(t[11:13])
            if (t.startswith(tonight) and hour >= 21) or (t.startswith(tomorrow) and hour <= 5):
                window.append(i)

        if not window:
            return "Unable to evaluate — no forecast data for tonight's imaging window."

        avg_cloud  = sum(clouds[i] for i in window) / len(window)
        max_cloud  = max(clouds[i] for i in window)
        max_wind   = max(wind[i] for i in window)
        max_precip = max(precip_prob[i] for i in window)
        avg_humid  = sum(humidity[i] for i in window) / len(window)

        reasons = []
        if max_cloud >= 80:
            reasons.append(f"cloud cover peaks at {int(max_cloud)}%")
        elif avg_cloud >= 40:
            reasons.append(f"average cloud cover {int(avg_cloud)}%")
        if max_precip >= 20:
            reasons.append(f"precipitation chance {int(max_precip)}%")
        if max_wind >= 20:
            reasons.append(f"wind up to {int(max_wind)} mph")
        if avg_humid >= 90:
            reasons.append(f"humidity {int(avg_humid)}%")

        if reasons:
            return "NO-GO — " + ", ".join(reasons) + "."
        else:
            detail = f"clouds averaging {int(avg_cloud)}%, wind {int(max_wind)} mph, humidity {int(avg_humid)}%"
            return f"GO — {detail}."

    except Exception as e:
        return f"GO/NO-GO unavailable: {e}"

def _run_tool(name: str, args: dict = None) -> str:
    if args is None:
        args = {}
    try:
        if name == "get_weather":
            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={OBS_LAT}&longitude={OBS_LON}"
                f"&hourly=cloudcover,windspeed_10m,relativehumidity_2m,"
                f"dewpoint_2m,precipitation_probability,temperature_2m"
                f"&current_weather=true&temperature_unit=fahrenheit"
                f"&wind_speed_unit=mph&timezone=America%2FNew_York&forecast_days=2"
            )
            r = requests.get(url, timeout=10)
            return json.dumps(r.json(), indent=2)
        elif name == "get_telescope_status":
            r = requests.get("http://192.168.50.245:1888/v2/api/equipment/mount/info", timeout=5)
            return r.text
        elif name == "get_guiding_state":
            import socket as _socket
            s = _socket.create_connection(("192.168.50.245", 4400), timeout=5)
            cmd = json.dumps({"method": "get_app_state", "params": [], "id": 1}) + "\r\n"
            s.sendall(cmd.encode())
            # PHD2 sends Version + AppState events first — read until we get "result"
            buffer = b""
            for _ in range(10):
                buffer += s.recv(4096)
                for line in buffer.split(b"\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        if "result" in msg or "error" in msg:
                            s.close()
                            return json.dumps(msg)
                    except Exception:
                        pass
            s.close()
            return json.dumps({"error": "no result from PHD2"})
        elif name == "get_camera_status":
            r = requests.get("http://192.168.50.245:1888/v2/api/equipment/camera/info", timeout=5)
            return r.text
        elif name == "send_notification":
            t = str(args.get("title", "ATLAS")).replace('"', '').replace("'", '')
            m = str(args.get("message", "")).replace('"', '').replace("'", '')
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                f'$n.BalloonTipTitle = "{t}"; '
                f'$n.BalloonTipText = "{m}"; '
                "$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info; "
                "$n.Visible = $true; "
                "$n.ShowBalloonTip(8000); "
                "Start-Sleep -Seconds 4; "
                "$n.Dispose()"
            )
            result = subprocess.run(
                ["powershell", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=15
            )
            return "Notification sent." if result.returncode == 0 else f"Notification failed: {result.stderr.strip()}"
        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        return f"Tool unavailable: {e}"

# ── Content block helper ──────────────────────────────────────────────────────

def _block_to_dict(block) -> dict:
    """Convert an Anthropic content block object to a plain dict for message history."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    elif block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    elif block.type == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    return {"type": block.type}

# ── Claude chat with tool support ─────────────────────────────────────────────

def _first_sentences(text: str, max_sentences: int = 2) -> str:
    """Keep only the first N sentences so TTS stays concise."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return " ".join(sentences[:max_sentences])


NOTIFY_LOG = Path(r"C:\Users\nasan\Desktop\ATLAS Observatory\ATLAS_Notifications.txt")

def _notify(title: str, message: str) -> None:
    """Show a dialog box AND append to persistent notification log on the desktop."""
    t = title.replace('"', '').replace("'", '')
    m = message.replace('"', '').replace("'", '')

    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f'[System.Windows.Forms.MessageBox]::Show("{m}", "{t}", '
        "[System.Windows.Forms.MessageBoxButtons]::OK, "
        "[System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null"
    )
    subprocess.Popen(
        ["powershell", "-NonInteractive", "-Command", ps],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    NOTIFY_LOG.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(NOTIFY_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}]  {title}\n{message}\n\n")


NOTIFY_KEYWORDS = {"notification", "notify", "toast", "alert", "remind"}


def chat(conversation: list, system_prompt: str, client: anthropic.Anthropic) -> str:
    """
    Call Claude with tool-use support.

    conversation : running user/assistant history (user message already appended by caller).
                   This function appends the final assistant reply before returning.
    system_prompt: the static system prompt (cached by Claude for efficiency).
    client       : the Anthropic synchronous client.

    Returns the assistant's final text response.
    """
    try:
        # Work on a copy so intermediate tool-use turns don't pollute conversation history
        working = list(conversation)

        for _ in range(5):
            response = client.messages.create(
                model=CHAT_MODEL,
                max_tokens=512,
                thinking={"type": "disabled"},
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},  # cache the large memory block
                    }
                ],
                tools=VOICE_TOOLS,
                messages=working,
            )

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            text_blocks     = [b for b in response.content if b.type == "text"]

            if not tool_use_blocks:
                # No tool calls — this is the final answer
                answer = text_blocks[0].text.strip() if text_blocks else ""
                conversation.append({"role": "assistant", "content": answer})
                return answer

            # Add full assistant turn (may include text + tool_use blocks) to working history
            working.append({
                "role": "assistant",
                "content": [_block_to_dict(b) for b in response.content],
            })

            # Execute each tool and collect results
            tool_results = []
            for block in tool_use_blocks:
                result = _run_tool(block.name, dict(block.input))
                print(f"[TOOL] {block.name}", flush=True)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            # Feed results back as a user turn
            working.append({"role": "user", "content": tool_results})

        # Max iterations reached — ask Claude to give a final answer without tools
        response = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=256,
            thinking={"type": "disabled"},
            system=system_prompt,
            messages=working,
        )
        text_blocks = [b for b in response.content if b.type == "text"]
        answer = text_blocks[0].text.strip() if text_blocks else "I was unable to retrieve that information."
        conversation.append({"role": "assistant", "content": answer})
        return answer

    except Exception as e:
        return f"I'm having trouble connecting to my AI system. {e}"

# ── System prompt ─────────────────────────────────────────────────────────────

def build_system_prompt(memory: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""You are ATLAS (Automated Telescope & Long-term Astronomy System), \
the autonomous observatory agent for Silver Springs Observatory, Florida.

Current date and time: {now}

VOICE RULES — these are absolute:
- Responses are spoken aloud. Write ONLY natural spoken sentences.
- No bullet points, no dashes, no asterisks, no numbered lists, no headers, no colons followed by lists.
- One to three sentences maximum per response. Be direct and concise.
- Never give summaries, recommendations, or multiple items. Answer the specific question asked and stop.

TOOL RULES — follow exactly:
- When the operator asks about weather, temperature, humidity, clouds, wind, or sky conditions: call get_weather, then answer in one sentence.
- When the operator asks about go/no-go, session planning, or imaging tonight: call get_weather, \
then answer with GO or NO-GO as the very first word, followed by the single most important reason. \
Example: "GO — skies are clear with low humidity and good seeing." or \
"NO-GO — cloud cover hits 100 percent by 9 PM."
- When the operator asks about the telescope, mount, pointing, tracking, or slewing: call get_telescope_status, then answer in one sentence.
- When the operator asks about guiding, PHD2, RMS, guide star, or guiding state: call get_guiding_state, then answer in one sentence using the PHD2 state definitions below.
- When the operator asks about the camera, imaging camera, sensor, or camera temperature: call get_camera_status, then answer in one sentence.
- When the operator asks about equipment, systems, or observatory status in general: call get_telescope_status and get_guiding_state, then summarise in two sentences.

PHD2 STATE DEFINITIONS — use these exact meanings when reporting guiding state:
- "Stopped"      : PHD2 is idle, not looping, not guiding.
- "Looping"      : PHD2 is capturing frames from the guide camera but no star is selected yet.
- "Selected"     : PHD2 is looping and a guide star has been selected — ready to calibrate or guide.
- "Calibrating"  : PHD2 is running the calibration routine.
- "Guiding"      : PHD2 is actively autoguiding — this is the normal imaging state.
- "LostLock"     : PHD2 lost the guide star and has paused guiding.
- "Paused"       : Guiding is temporarily paused by the user or sequence.
- "StarSelected" : Same as Selected — star chosen, ready to guide.

CAMERA DISTINCTION — critical:
- The NINA camera (get_camera_status) is the IMAGING camera (ASI 585MC Pro). It connects through NINA.
- The GUIDE camera (SVBony SV205) connects through PHD2, NOT through NINA. Never say the guide camera
  is disconnected based on NINA camera status — they are separate devices on separate software.
- When the operator asks you to send a notification: ALWAYS call send_notification. Do not skip it.
- When the operator asks to test notifications: call send_notification immediately with title "ATLAS Test" \
and message "Notification system is working." Do not describe what you would do — just call the tool.
- After calling send_notification, confirm verbally in one short sentence that it was sent.
- NEVER say a status is unknown without first calling the appropriate tool to check it.

You know this observatory intimately. Be direct and confident, like a trusted colleague.

For hardware commands (slew, start guiding, start a sequence), explain those require \
the full agent session.

Your memory and observatory state:
{memory}"""

# ── Status display ────────────────────────────────────────────────────────────

STATUS_IDLE       = "◌  Listening..."
STATUS_SPEECH     = "●  Hearing you..."
STATUS_PROCESSING = "■  Thinking..."
STATUS_SPEAKING   = "♪  ATLAS speaking..."

def status(msg: str) -> None:
    print(f"\r{msg:<40}", end="", flush=True)

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "=" * 52)
    print("  ATLAS  —  Live Voice Interface")
    print("=" * 52)

    # ── API key setup ──
    get_api_key()

    # ── Init Anthropic client ──
    print("\nConnecting to Claude...", flush=True)
    try:
        _client = anthropic.Anthropic()
        # Quick connectivity check
        _client.messages.create(
            model=CHAT_MODEL,
            max_tokens=10,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": "ping"}],
        )
        print(f"Claude ({ANTHROPIC_MODEL}): connected", flush=True)
    except Exception as e:
        print(f"ERROR: Cannot reach Claude API — {e}", flush=True)
        sys.exit(1)

    # ── Init TTS ──
    print("Initialising voice output...", flush=True)

    # ── Load Whisper ──
    print("Loading Whisper (small)...", flush=True)
    whisper = WhisperModel("small", device="cpu", compute_type="int8")
    print("Whisper: ready", flush=True)

    # ── Load memory ──
    print("Loading observatory memory...", flush=True)
    memory = load_memory()
    system_prompt = build_system_prompt(memory)

    # conversation holds user/assistant turns only (system goes to API param)
    conversation: list = []

    # ── Calibrate ambient noise ──
    print(f"\nCalibrating ambient noise ({CALIBRATION_SECS:.0f}s) — stay quiet...", flush=True)
    cal_frames = []
    cal_done   = threading.Event()

    def cal_callback(indata, frames, time_info, status_flags):
        cal_frames.append(indata.copy().flatten())
        if len(cal_frames) * CHUNK_FRAMES / SAMPLE_RATE >= CALIBRATION_SECS:
            cal_done.set()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=CHUNK_FRAMES, callback=cal_callback):
        cal_done.wait()

    ambient_rms   = float(np.sqrt(np.mean(np.concatenate(cal_frames) ** 2)))
    vad_threshold = ambient_rms * NOISE_MULTIPLIER
    print(f"Ambient RMS: {ambient_rms:.5f}  |  Speech threshold: {vad_threshold:.5f}", flush=True)

    # ── Audio queue and state ──
    audio_q  = queue.Queue()
    busy     = threading.Event()   # set while ATLAS is processing or speaking

    def audio_callback(indata, frames, time_info, status_flags):
        if not busy.is_set():
            audio_q.put(indata.copy().flatten())

    # ── Greeting ──
    print("\nATLAS voice interface active. Speak naturally.\n", flush=True)
    busy.set()
    speak("ATLAS online. I'm listening.")
    busy.clear()

    # ── Conversation loop ──
    speech_chunks  = []
    silence_chunks = 0
    in_speech      = False
    silence_limit  = int(SILENCE_DURATION * SAMPLE_RATE / CHUNK_FRAMES)

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=CHUNK_FRAMES, callback=audio_callback):

        status(STATUS_IDLE)

        while True:
            try:
                chunk = audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            except KeyboardInterrupt:
                break

            rms = float(np.sqrt(np.mean(chunk ** 2)))

            if rms > vad_threshold:
                # Speech detected
                if not in_speech:
                    in_speech = True
                    speech_chunks = []
                    silence_chunks = 0
                    status(STATUS_SPEECH)
                speech_chunks.append(chunk)
                silence_chunks = 0

            elif in_speech:
                # In speech but quiet — count silence
                speech_chunks.append(chunk)
                silence_chunks += 1

                if silence_chunks >= silence_limit:
                    # Utterance ended — process it
                    in_speech = False
                    busy.set()

                    audio = np.concatenate(speech_chunks)
                    duration = len(audio) / SAMPLE_RATE
                    speech_chunks = []
                    silence_chunks = 0

                    # Drain queue (discard audio captured during processing)
                    while not audio_q.empty():
                        try:
                            audio_q.get_nowait()
                        except queue.Empty:
                            break

                    if duration < MIN_SPEECH_SECS:
                        busy.clear()
                        status(STATUS_IDLE)
                        continue

                    # Transcribe
                    status(STATUS_PROCESSING)
                    segments, _ = whisper.transcribe(audio, language="en", beam_size=5)
                    text = " ".join(s.text.strip() for s in segments).strip()

                    if not text:
                        busy.clear()
                        status(STATUS_IDLE)
                        continue

                    print(f"\nYou:   {text}", flush=True)

                    # Exit condition
                    if any(w in text.lower() for w in ["goodbye", "good bye", "exit atlas",
                                                        "shut down", "go offline", "close voice"]):
                        speak("Understood. ATLAS going offline. Clear skies.")
                        break

                    # Strip punctuation for reliable keyword matching
                    words = set(re.sub(r"[^a-z]", " ", text.lower()).split())
                    is_go_no_go  = bool(words & GO_NO_GO_KEYWORDS)
                    wants_notify = bool(words & NOTIFY_KEYWORDS)
                    print(f"[DEBUG] words={words & (GO_NO_GO_KEYWORDS | NOTIFY_KEYWORDS)}  go_no_go={is_go_no_go}  notify={wants_notify}", flush=True)

                    # For go/no-go: evaluate in Python — deterministic, no LLM needed
                    if is_go_no_go:
                        verdict = _evaluate_go_no_go()
                        spoken  = verdict
                        # Always fire a notification for go/no-go
                        _notify("ATLAS — Tonight's Session", verdict)
                        conversation.append({"role": "user",      "content": text})
                        conversation.append({"role": "assistant", "content": verdict})
                    else:
                        # Normal chat path — Claude handles tool use
                        conversation.append({"role": "user", "content": text})
                        response = chat(conversation, system_prompt, _client)
                        # chat() has already appended the assistant reply to conversation
                        spoken = _first_sentences(_strip_markdown(response), max_sentences=2)
                        if wants_notify:
                            _notify("ATLAS", spoken)

                    # Speak
                    status(STATUS_SPEAKING)
                    speak(spoken)

                    # Trim conversation to last 20 turns to stay efficient
                    if len(conversation) > 20:
                        conversation[:] = conversation[-20:]

                    # Clear any audio captured during response
                    while not audio_q.empty():
                        try:
                            audio_q.get_nowait()
                        except queue.Empty:
                            break

                    busy.clear()
                    status(STATUS_IDLE)

            else:
                # Quiet and not in speech
                status(STATUS_IDLE)

    print("\n\nATLAS voice interface closed.", flush=True)


if __name__ == "__main__":
    main()
