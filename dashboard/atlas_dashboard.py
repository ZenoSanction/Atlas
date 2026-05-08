"""
ATLAS Dashboard
===============
Warm room desktop control panel for the ATLAS Observatory System.
Connects to atlas_server.exe running on the observatory PC over the LAN.

Dependencies:
    pip install -r requirements.txt

Usage:
    python atlas_dashboard.py
"""

import json
import threading
import datetime
import queue
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path

import requests
import subprocess

# ---------------------------------------------------------------------------
# Config — written by installer, read on startup
# ---------------------------------------------------------------------------
CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "observatory_ip":   "192.168.50.245",
    "observatory_name": "Silver Springs Observatory",
    "obs_lat":          29.2274,
    "obs_lon":          -82.0604,
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

CONFIG = load_config()
SERVER = f"http://{CONFIG['observatory_ip']}:5000"

# ---------------------------------------------------------------------------
# Time / unit helpers
# ---------------------------------------------------------------------------
def _fmt_time(time_str: str) -> str:
    """Convert an ISO datetime string or HH:MM to 12-hour AM/PM format (e.g. 9:00PM)."""
    try:
        # Handle "2026-05-08T21:00", "2026-05-08T21:00:00.000", or bare "21:00"
        if "T" in time_str:
            hhmm = time_str[11:16]
        else:
            hhmm = time_str[-5:]
        h, m = int(hhmm[:2]), int(hhmm[3:5])
        period = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d}{period}"
    except Exception:
        return time_str

def _fmt_datetime(iso: str) -> str:
    """Convert an ISO datetime string to readable 12-hour format (e.g. May 8  9:00 PM)."""
    try:
        dt = datetime.datetime.fromisoformat(iso)
        return dt.strftime("%b %-d  %I:%M %p").replace("  ", "  ")
    except Exception:
        return iso

# ---------------------------------------------------------------------------
# Colors & fonts
# ---------------------------------------------------------------------------
BG          = "#0d0d1a"
BG_PANEL    = "#13132b"
BG_CARD     = "#1a1a35"
FG          = "#e0e0f0"
FG_DIM      = "#7070a0"
ACCENT      = "#4a9eff"
GO_COLOR    = "#00c853"
CAUTION_COLOR = "#ffd600"
NOGO_COLOR  = "#ff1744"
UNKNOWN_COLOR = "#7070a0"

FONT_TITLE  = ("Segoe UI", 22, "bold")
FONT_HEAD   = ("Segoe UI", 13, "bold")
FONT_BODY   = ("Segoe UI", 11)
FONT_SMALL  = ("Segoe UI", 9)
FONT_MONO   = ("Consolas", 10)
FONT_BANNER = ("Segoe UI", 18, "bold")
FONT_REASON = ("Segoe UI", 11)

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
_session = requests.Session()

def api_get(path: str, timeout: int = 5) -> dict:
    try:
        r = _session.get(f"{SERVER}{path}", timeout=timeout)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def api_post(path: str, data: dict = None, timeout: int = 10) -> dict:
    try:
        r = _session.post(f"{SERVER}{path}", json=data or {}, timeout=timeout)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------------------
# Text-to-speech — uses Windows SAPI directly via win32com (instant, reliable)
# ---------------------------------------------------------------------------
_tts_queue: queue.Queue = queue.Queue()
_atlas_speaking = threading.Event()  # set while ATLAS is speaking — mutes listener

def _tts_worker():
    try:
        import win32com.client
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        speaker.Rate = 1
    except Exception:
        speaker = None

    while True:
        text = _tts_queue.get()
        if text is None:
            break
        if not text:
            continue
        try:
            _atlas_speaking.set()
            if speaker:
                speaker.Speak(text)
            else:
                safe = text.replace("'", " ").replace('"', " ")
                subprocess.run(
                    ["powershell", "-Command",
                     f"Add-Type -AssemblyName System.Speech; "
                     f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                     f"$s.Speak('{safe}')"],
                    capture_output=True
                )
        except Exception:
            pass
        finally:
            _atlas_speaking.clear()

_tts_thread = threading.Thread(target=_tts_worker, daemon=True)
_tts_thread.start()

def speak(text: str):
    _tts_queue.put(text)

# ---------------------------------------------------------------------------
# Speech-to-text (Whisper) — loaded lazily
# ---------------------------------------------------------------------------
_whisper_model = None

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        try:
            import whisper
            _whisper_model = whisper.load_model("tiny")
        except Exception:
            pass
    return _whisper_model

def record_and_transcribe() -> tuple[str, str]:
    """Record from microphone, stop automatically on silence, return (text, error)."""
    try:
        import sounddevice as sd
        import numpy as np

        sample_rate      = 16000
        chunk_frames     = int(sample_rate * 0.3)   # 300ms chunks
        speech_threshold = 0.015
        silence_needed   = 10                        # ~3s silence to stop
        max_chunks       = 100                       # hard cap ~30s

        audio_buffer  = []
        silence_count = 0
        speech_count  = 0
        recording     = False

        with sd.InputStream(samplerate=sample_rate, channels=1,
                            dtype="float32", blocksize=chunk_frames) as stream:
            for _ in range(max_chunks):
                chunk, _ = stream.read(chunk_frames)
                rms = float(np.sqrt(np.mean(chunk ** 2)))

                if rms > speech_threshold:
                    if not recording:
                        recording = True
                    audio_buffer.append(chunk.flatten())
                    speech_count  += 1
                    silence_count  = 0
                elif recording:
                    audio_buffer.append(chunk.flatten())
                    silence_count += 1
                    if silence_count >= silence_needed:
                        break  # silence detected — done

        if not audio_buffer or speech_count < 2:
            return "", "No speech detected — try speaking louder or closer to the mic."

        audio = np.concatenate(audio_buffer)
    except Exception as e:
        return "", f"Microphone error: {e}"

    try:
        model = get_whisper()
        if model is None:
            return "", "Whisper failed to load."
        result = model.transcribe(audio, fp16=False)
        text = result.get("text", "").strip()
        if not text:
            return "", "No speech detected — try speaking louder or closer to the mic."
        return text, ""
    except Exception as e:
        return "", f"Transcription error: {e}"

# ---------------------------------------------------------------------------
# Continuous voice listener
# ---------------------------------------------------------------------------
_listening_active = threading.Event()
_chat_busy        = threading.Event()  # set while waiting for ATLAS reply
_listen_callback = None
_listen_indicator_callback = None

def _continuous_listen_worker():
    """Monitors mic continuously, sends speech to ATLAS when detected."""
    try:
        import sounddevice as sd
        import numpy as np
    except Exception:
        return

    sample_rate  = 16000
    chunk_frames = int(sample_rate * 0.3)   # 300ms chunks
    silence_chunks_needed = 10              # ~3s silence to end utterance
    speech_threshold = 0.015               # RMS threshold for voice activity
    min_speech_chunks = 3                  # ignore taps < ~0.9s

    while True:
        _listening_active.wait()           # block until listening is enabled

        audio_buffer = []
        silence_count = 0
        speech_count  = 0
        recording     = False

        try:
            with sd.InputStream(samplerate=sample_rate, channels=1,
                                dtype="float32", blocksize=chunk_frames) as stream:
                while _listening_active.is_set():

                    # Don't listen while ATLAS is speaking
                    if _atlas_speaking.is_set():
                        time.sleep(0.1)
                        continue

                    chunk, _ = stream.read(chunk_frames)
                    rms = float(np.sqrt(np.mean(chunk ** 2)))

                    if rms > speech_threshold:
                        if not recording:
                            recording = True
                            audio_buffer = []
                        audio_buffer.append(chunk.flatten())
                        speech_count  += 1
                        silence_count  = 0
                    elif recording:
                        audio_buffer.append(chunk.flatten())
                        silence_count += 1
                        if silence_count >= silence_chunks_needed:
                            # End of utterance
                            if speech_count >= min_speech_chunks:
                                audio = np.concatenate(audio_buffer)
                                if _listen_indicator_callback:
                                    _listen_indicator_callback("⏳ Transcribing...")
                                threading.Thread(
                                    target=_transcribe_and_send,
                                    args=(audio,),
                                    daemon=True
                                ).start()
                            recording     = False
                            audio_buffer  = []
                            speech_count  = 0
                            silence_count = 0
        except Exception:
            time.sleep(1)

def _transcribe_and_send(audio):
    """Transcribe audio and send to ATLAS callback."""
    try:
        model = get_whisper()
        if model is None:
            return
        result = model.transcribe(audio, fp16=False)
        text = result.get("text", "").strip()
        if text and _listen_callback:
            _listen_callback(text)
    except Exception:
        pass

_listen_thread = threading.Thread(target=_continuous_listen_worker, daemon=True)
_listen_thread.start()

# ---------------------------------------------------------------------------
# Main Application Window
# ---------------------------------------------------------------------------
class ATLASDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"ATLAS — {CONFIG['observatory_name']}")
        self.geometry("1280x800")
        self.minsize(1024, 700)
        self.configure(bg=BG)

        self._poll_running = {}     # guard against overlapping poll threads
        self._build_ui()
        self._start_polling()

    # ── UI Construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        # Title bar
        title_bar = tk.Frame(self, bg=BG, pady=8)
        title_bar.pack(fill="x", padx=20)
        tk.Label(title_bar, text="⭐ ATLAS", font=FONT_TITLE,
                 bg=BG, fg=ACCENT).pack(side="left")
        tk.Label(title_bar, text=CONFIG["observatory_name"], font=FONT_BODY,
                 bg=BG, fg=FG_DIM).pack(side="left", padx=(12, 0), pady=(6, 0))
        self._clock_label = tk.Label(title_bar, text="", font=FONT_BODY,
                                     bg=BG, fg=FG_DIM)
        self._clock_label.pack(side="right")
        self._update_clock()

        # Notebook tabs
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_PANEL, foreground=FG,
                        font=FONT_BODY, padding=[14, 6])
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#ffffff")])

        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self._tab_overview  = self._make_tab("Overview")
        self._tab_telescope = self._make_tab("Telescope")
        self._tab_camera    = self._make_tab("Camera")
        self._tab_guiding   = self._make_tab("Guiding")
        self._tab_weather   = self._make_tab("Weather")
        self._tab_planning  = self._make_tab("Planning")
        self._tab_session   = self._make_tab("Session")
        self._tab_watchdog  = self._make_tab("Watchdog")
        self._tab_atlas     = self._make_tab("ATLAS")

        self._build_overview()
        self._build_telescope()
        self._build_camera()
        self._build_guiding()
        self._build_weather()
        self._build_planning()
        self._build_session()
        self._build_watchdog()
        self._build_atlas()

    def _make_tab(self, name: str) -> tk.Frame:
        frame = tk.Frame(self._nb, bg=BG)
        self._nb.add(frame, text=f"  {name}  ")
        return frame

    def _card(self, parent, title: str, row: int, col: int,
              rowspan: int = 1, colspan: int = 1) -> tk.Frame:
        frame = tk.LabelFrame(parent, text=f" {title} ", font=FONT_SMALL,
                              bg=BG_CARD, fg=FG_DIM, bd=1, relief="solid",
                              padx=10, pady=8)
        frame.grid(row=row, column=col, rowspan=rowspan, columnspan=colspan,
                   padx=6, pady=6, sticky="nsew")
        return frame

    def _label_pair(self, parent, label: str, row: int) -> tk.Label:
        tk.Label(parent, text=label, font=FONT_SMALL, bg=BG_CARD,
                 fg=FG_DIM, anchor="w").grid(row=row, column=0, sticky="w", pady=1)
        val = tk.Label(parent, text="—", font=FONT_BODY, bg=BG_CARD,
                       fg=FG, anchor="w")
        val.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=1)
        return val

    def _btn(self, parent, text: str, command, color: str = ACCENT,
             width: int = 14) -> tk.Button:
        return tk.Button(parent, text=text, command=command,
                         bg=color, fg="#ffffff", font=FONT_BODY,
                         relief="flat", activebackground=color,
                         cursor="hand2", width=width, pady=4)

    # ── Overview Tab ─────────────────────────────────────────────────────────

    def _build_overview(self):
        f = self._tab_overview
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        # GO/NO-GO Banner
        self._banner_frame = tk.Frame(f, bg=UNKNOWN_COLOR, pady=18)
        self._banner_frame.grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 8))
        self._banner_frame.columnconfigure(0, weight=1)

        self._banner_verdict = tk.Label(self._banner_frame, text="● UNKNOWN",
                                        font=FONT_BANNER, bg=UNKNOWN_COLOR, fg="#ffffff")
        self._banner_verdict.grid(row=0, column=0)

        self._banner_reason = tk.Label(self._banner_frame, text="Connecting to observatory...",
                                       font=FONT_REASON, bg=UNKNOWN_COLOR, fg="#dddddd")
        self._banner_reason.grid(row=1, column=0, pady=(4, 0))

        self._banner_time = tk.Label(self._banner_frame, text="",
                                     font=FONT_SMALL, bg=UNKNOWN_COLOR, fg="#aaaaaa")
        self._banner_time.grid(row=2, column=0)

        refresh_btn = tk.Button(self._banner_frame, text="↻ Refresh",
                                command=self._force_status_refresh,
                                bg="#555577", fg="#ffffff", font=FONT_SMALL,
                                relief="flat", cursor="hand2", padx=8, pady=2)
        refresh_btn.grid(row=0, column=1, padx=20)

        # Status grid
        grid = tk.Frame(f, bg=BG)
        grid.grid(row=1, column=0, sticky="nsew", padx=10)
        for i in range(3):
            grid.columnconfigure(i, weight=1)
        for i in range(2):
            grid.rowconfigure(i, weight=1)

        # Telescope card
        tc = self._card(grid, "Telescope", 0, 0)
        tc.columnconfigure(1, weight=1)
        self._ov_tel_status = self._label_pair(tc, "Status", 0)
        self._ov_tel_ra     = self._label_pair(tc, "RA", 1)
        self._ov_tel_dec    = self._label_pair(tc, "Dec", 2)
        self._ov_tel_alt    = self._label_pair(tc, "Altitude", 3)

        # Camera card
        cc = self._card(grid, "Camera", 0, 1)
        cc.columnconfigure(1, weight=1)
        self._ov_cam_status = self._label_pair(cc, "Status", 0)
        self._ov_cam_exp    = self._label_pair(cc, "Exposing", 1)
        self._ov_cam_temp   = self._label_pair(cc, "Temperature", 2)

        # Guiding card
        gc = self._card(grid, "Guiding", 0, 2)
        gc.columnconfigure(1, weight=1)
        self._ov_guide_state = self._label_pair(gc, "State", 0)
        self._ov_guide_rms   = self._label_pair(gc, "RMS Total", 1)
        self._ov_guide_ra    = self._label_pair(gc, "RMS RA", 2)
        self._ov_guide_dec   = self._label_pair(gc, "RMS Dec", 3)

        # Weather card
        wc = self._card(grid, "Weather", 1, 0)
        wc.columnconfigure(1, weight=1)
        self._ov_wx_cloud = self._label_pair(wc, "Cloud Cover", 0)
        self._ov_wx_wind  = self._label_pair(wc, "Wind", 1)
        self._ov_wx_humid = self._label_pair(wc, "Humidity", 2)
        self._ov_wx_dew   = self._label_pair(wc, "Dew Spread", 3)

        # Session card
        sc = self._card(grid, "Session", 1, 1)
        sc.columnconfigure(1, weight=1)
        self._ov_seq_status = self._label_pair(sc, "Sequence", 0)
        self._ov_watchdog   = self._label_pair(sc, "Watchdog", 1)
        self._ov_moon       = self._label_pair(sc, "Moon Phase", 2)

        # Quick controls card
        qc = self._card(grid, "Quick Controls", 1, 2)
        self._btn(qc, "▶ Start Sequence", self._start_sequence, GO_COLOR).pack(fill="x", pady=2)
        self._btn(qc, "■ Stop Sequence",  self._stop_sequence,  NOGO_COLOR).pack(fill="x", pady=2)
        self._btn(qc, "⊙ Park Telescope", self._park_telescope, "#555577").pack(fill="x", pady=2)
        self._btn(qc, "↻ Force Status",   self._force_status_refresh, "#555577").pack(fill="x", pady=2)

    def _update_banner(self, verdict: str, reason: str, updated: str = ""):
        colors = {
            "GO":      GO_COLOR,
            "CAUTION": CAUTION_COLOR,
            "NO-GO":   NOGO_COLOR,
        }
        # CAUTION is yellow — use dark text for contrast; all others use white
        text_colors = {
            "GO":      "#ffffff",
            "CAUTION": "#1a1a00",
            "NO-GO":   "#ffffff",
        }
        symbols = {"GO": "●", "CAUTION": "◆", "NO-GO": "✖"}
        color    = colors.get(verdict, UNKNOWN_COLOR)
        fg       = text_colors.get(verdict, "#ffffff")
        sym      = symbols.get(verdict, "●")

        self._banner_frame.configure(bg=color)
        self._banner_verdict.configure(text=f"{sym}  {verdict}", bg=color, fg=fg)
        self._banner_reason.configure(text=reason, bg=color, fg=fg)
        self._banner_time.configure(
            text=f"Last assessed: {_fmt_datetime(updated)}" if updated else "",
            bg=color, fg=fg)

    # ── Telescope Tab ────────────────────────────────────────────────────────

    def _build_telescope(self):
        f = self._tab_telescope
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(0, weight=1)

        # Status card
        sc = self._card(f, "Telescope Status", 0, 0)
        sc.columnconfigure(1, weight=1)
        self._tel_connected  = self._label_pair(sc, "Connected", 0)
        self._tel_ra         = self._label_pair(sc, "RA", 1)
        self._tel_dec        = self._label_pair(sc, "Dec", 2)
        self._tel_alt        = self._label_pair(sc, "Altitude", 3)
        self._tel_az         = self._label_pair(sc, "Azimuth", 4)
        self._tel_tracking   = self._label_pair(sc, "Tracking", 5)
        self._tel_slewing    = self._label_pair(sc, "Slewing", 6)
        self._tel_pier       = self._label_pair(sc, "Pier Side", 7)

        # Controls card
        cc = self._card(f, "Controls", 0, 1)
        tk.Label(cc, text="Slew to Target", font=FONT_SMALL,
                 bg=BG_CARD, fg=FG_DIM).pack(anchor="w")
        self._slew_entry = tk.Entry(cc, font=FONT_BODY, bg=BG_PANEL,
                                    fg=FG, insertbackground=FG,
                                    relief="flat")
        self._slew_entry.pack(fill="x", pady=(2, 8))
        self._btn(cc, "Slew to Target", self._slew_to_target, ACCENT, 20).pack(fill="x", pady=2)
        ttk.Separator(cc, orient="horizontal").pack(fill="x", pady=10)
        self._btn(cc, "⊙ Park",   self._park_telescope,   "#555577", 20).pack(fill="x", pady=2)
        self._btn(cc, "⊕ Unpark", self._unpark_telescope, "#555577", 20).pack(fill="x", pady=2)

    # ── Camera Tab ───────────────────────────────────────────────────────────

    def _build_camera(self):
        f = self._tab_camera
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)

        sc = self._card(f, "Camera Status", 0, 0)
        sc.columnconfigure(1, weight=1)
        self._cam_connected   = self._label_pair(sc, "Connected", 0)
        self._cam_name        = self._label_pair(sc, "Camera", 1)
        self._cam_temp        = self._label_pair(sc, "Temperature", 2)
        self._cam_gain        = self._label_pair(sc, "Gain", 3)
        self._cam_offset      = self._label_pair(sc, "Offset", 4)
        self._cam_binning     = self._label_pair(sc, "Binning", 5)
        self._cam_exposing    = self._label_pair(sc, "Exposing", 6)
        self._cam_exposure    = self._label_pair(sc, "Exposure Time", 7)

        # Focuser
        fc = self._card(f, "Focuser", 0, 0)
        # Re-position below camera card by using row 1
        fc.grid_forget()
        fc = self._card(f, "Focuser", 1, 0)
        fc.columnconfigure(1, weight=1)
        self._foc_position = self._label_pair(fc, "Position", 0)
        self._foc_moving   = self._label_pair(fc, "Moving", 1)
        self._foc_temp     = self._label_pair(fc, "Temperature", 2)

        tk.Label(fc, text="Move to position:", font=FONT_SMALL,
                 bg=BG_CARD, fg=FG_DIM).grid(row=3, column=0, sticky="w", pady=(8, 0))
        self._foc_entry = tk.Entry(fc, font=FONT_BODY, bg=BG_PANEL,
                                   fg=FG, insertbackground=FG, relief="flat", width=10)
        self._foc_entry.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        self._btn(fc, "Move Focuser", self._move_focuser, ACCENT, 14).grid(
            row=4, column=0, columnspan=2, pady=(6, 0), sticky="w")

    # ── Guiding Tab ──────────────────────────────────────────────────────────

    def _build_guiding(self):
        f = self._tab_guiding
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=2)
        f.rowconfigure(0, weight=1)

        # Stats card
        sc = self._card(f, "Guiding Stats", 0, 0)
        sc.columnconfigure(1, weight=1)
        self._guide_state    = self._label_pair(sc, "State", 0)
        self._guide_rms      = self._label_pair(sc, "RMS Total", 1)
        self._guide_rms_ra   = self._label_pair(sc, "RMS RA", 2)
        self._guide_rms_dec  = self._label_pair(sc, "RMS Dec", 3)
        self._guide_peak_ra  = self._label_pair(sc, "Peak RA", 4)
        self._guide_peak_dec = self._label_pair(sc, "Peak Dec", 5)
        self._guide_snr      = self._label_pair(sc, "Guide Star SNR", 6)

        ttk.Separator(sc, orient="horizontal").grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=10)
        self._btn(sc, "▶ Start Guiding", self._start_guiding, GO_COLOR, 16).grid(
            row=8, column=0, columnspan=2, sticky="ew", pady=2)
        self._btn(sc, "■ Stop Guiding", self._stop_guiding, NOGO_COLOR, 16).grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=2)

        # Graph card
        gc = self._card(f, "Guiding Graph (RA/Dec Error)", 0, 1)
        gc.columnconfigure(0, weight=1)
        gc.rowconfigure(0, weight=1)

        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

            fig = Figure(figsize=(6, 3), dpi=90, facecolor=BG_CARD)
            self._guide_ax = fig.add_subplot(111)
            self._guide_ax.set_facecolor(BG_PANEL)
            self._guide_ax.tick_params(colors=FG_DIM)
            self._guide_ax.spines[:].set_color(BG_PANEL)
            self._guide_ax.set_ylabel("Error (arcsec)", color=FG_DIM, fontsize=8)
            self._guide_ax.axhline(0, color=FG_DIM, linewidth=0.5)
            self._guide_ra_line,  = self._guide_ax.plot([], [], color="#4a9eff", label="RA")
            self._guide_dec_line, = self._guide_ax.plot([], [], color="#ff9800", label="Dec")
            self._guide_ax.legend(facecolor=BG_CARD, labelcolor=FG, fontsize=8)
            fig.tight_layout()

            canvas = FigureCanvasTkAgg(fig, gc)
            canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
            self._guide_canvas = canvas
            self._guide_ra_data  = []
            self._guide_dec_data = []
        except ImportError:
            tk.Label(gc, text="Install matplotlib for live graph",
                     font=FONT_BODY, bg=BG_CARD, fg=FG_DIM).grid(row=0, column=0)
            self._guide_canvas = None

    # ── Weather Tab ──────────────────────────────────────────────────────────

    def _build_weather(self):
        f = self._tab_weather
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=2)
        f.rowconfigure(0, weight=1)

        # Current conditions
        wc = self._card(f, "Current Conditions", 0, 0)
        wc.columnconfigure(1, weight=1)
        self._wx_verdict  = self._label_pair(wc, "Verdict", 0)
        self._wx_reason   = self._label_pair(wc, "Reason", 1)
        self._wx_cloud    = self._label_pair(wc, "Cloud Cover", 2)
        self._wx_temp     = self._label_pair(wc, "Temperature", 3)
        self._wx_humid    = self._label_pair(wc, "Humidity", 4)
        self._wx_dew      = self._label_pair(wc, "Dew Point", 5)
        self._wx_spread   = self._label_pair(wc, "Dew Spread", 6)
        self._wx_wind     = self._label_pair(wc, "Wind Speed", 7)
        self._wx_gusts    = self._label_pair(wc, "Wind Gusts", 8)
        self._wx_precip   = self._label_pair(wc, "Precipitation", 9)
        self._wx_pressure = self._label_pair(wc, "Pressure", 10)

        # Forecast
        fc = self._card(f, "Hourly Forecast", 0, 1)
        fc.columnconfigure(0, weight=1)
        fc.rowconfigure(0, weight=1)
        self._forecast_text = scrolledtext.ScrolledText(
            fc, font=FONT_MONO, bg=BG_PANEL, fg=FG,
            insertbackground=FG, relief="flat", state="disabled")
        self._forecast_text.grid(row=0, column=0, sticky="nsew")
        self._btn(fc, "↻ Refresh Forecast", self._refresh_forecast,
                  ACCENT, 18).grid(row=1, column=0, pady=(6, 0), sticky="w")

    # ── Planning Tab ─────────────────────────────────────────────────────────

    def _build_planning(self):
        f = self._tab_planning
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(1, weight=1)
        f.rowconfigure(2, weight=2)

        # Moon info
        mc = self._card(f, "Moon", 0, 0)
        mc.columnconfigure(1, weight=1)
        self._plan_moon_phase = self._label_pair(mc, "Phase", 0)
        self._plan_moon_illum = self._label_pair(mc, "Illumination", 1)

        # Object lookup
        oc = self._card(f, "Object Lookup", 0, 1)
        oc.columnconfigure(1, weight=1)
        tk.Label(oc, text="Object name:", font=FONT_SMALL,
                 bg=BG_CARD, fg=FG_DIM).grid(row=0, column=0, sticky="w")
        self._lookup_entry = tk.Entry(oc, font=FONT_BODY, bg=BG_PANEL,
                                      fg=FG, insertbackground=FG, relief="flat")
        self._lookup_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._btn(oc, "Look Up", self._lookup_object, ACCENT, 10).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Hourly forecast
        rc = self._card(f, "Tonight's Hourly Forecast", 1, 0, colspan=2)
        rc.columnconfigure(0, weight=1)
        rc.rowconfigure(0, weight=1)
        self._targets_text = scrolledtext.ScrolledText(
            rc, font=FONT_MONO, bg=BG_PANEL, fg=FG,
            insertbackground=FG, relief="flat", state="disabled")
        self._targets_text.grid(row=0, column=0, sticky="nsew")
        self._btn(rc, "↻ Refresh Targets", self._refresh_targets,
                  ACCENT, 18).grid(row=1, column=0, pady=(6, 0), sticky="w")

        # ATLAS Session Plan
        pc = self._card(f, "ATLAS Session Plan", 2, 0, colspan=2)
        pc.columnconfigure(0, weight=1)
        pc.rowconfigure(0, weight=1)
        self._plan_text = scrolledtext.ScrolledText(
            pc, font=FONT_MONO, bg=BG_PANEL, fg=FG,
            insertbackground=FG, relief="flat", state="disabled", wrap="word")
        self._plan_text.grid(row=0, column=0, sticky="nsew")

        btn_row = tk.Frame(pc, bg=BG_CARD)
        btn_row.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._plan_btn = self._btn(btn_row, "✦ Generate Session Plan",
                                   self._generate_session_plan, ACCENT, 22)
        self._plan_btn.pack(side="left")
        self._plan_status = tk.Label(btn_row, text="", font=FONT_SMALL,
                                     bg=BG_CARD, fg=FG_DIM)
        self._plan_status.pack(side="left", padx=(12, 0))

    # ── Session Tab ──────────────────────────────────────────────────────────

    def _build_session(self):
        f = self._tab_session
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(1, weight=1)

        # Controls
        cc = self._card(f, "Session Controls", 0, 0)
        self._btn(cc, "✔ Pre-Session Check", self._pre_session_check, ACCENT, 22).pack(fill="x", pady=3)
        self._btn(cc, "▶ Start Sequence",    self._start_sequence,    GO_COLOR, 22).pack(fill="x", pady=3)
        self._btn(cc, "■ Stop Sequence",     self._stop_sequence,     NOGO_COLOR, 22).pack(fill="x", pady=3)
        self._btn(cc, "⊙ Park Telescope",   self._park_telescope,    "#555577", 22).pack(fill="x", pady=3)

        # Sequence status
        sc = self._card(f, "Sequence Status", 0, 1)
        sc.columnconfigure(1, weight=1)
        self._seq_status   = self._label_pair(sc, "Status", 0)
        self._seq_target   = self._label_pair(sc, "Target", 1)
        self._seq_progress = self._label_pair(sc, "Progress", 2)

        # Session log
        lc = self._card(f, "Session Log", 1, 0, colspan=2)
        lc.columnconfigure(0, weight=1)
        lc.rowconfigure(0, weight=1)
        self._session_log = scrolledtext.ScrolledText(
            lc, font=FONT_MONO, bg=BG_PANEL, fg=FG,
            insertbackground=FG, relief="flat", state="disabled")
        self._session_log.grid(row=0, column=0, sticky="nsew")

    # ── Watchdog Tab ─────────────────────────────────────────────────────────

    def _build_watchdog(self):
        f = self._tab_watchdog
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=2)
        f.rowconfigure(1, weight=1)

        self._wd_thresholds_loaded = False  # populate entries only on first poll

        # ── Controls card ──
        cc = self._card(f, "Watchdog Controls", 0, 0)

        self._wd_status_label = tk.Label(cc, text="● STOPPED", font=FONT_HEAD,
                                          bg=BG_CARD, fg=NOGO_COLOR)
        self._wd_status_label.pack(pady=(0, 10))

        self._btn(cc, "▶ Start Watchdog", self._start_watchdog, GO_COLOR,  18).pack(fill="x", pady=2)
        self._btn(cc, "■ Stop Watchdog",  self._stop_watchdog,  NOGO_COLOR, 18).pack(fill="x", pady=2)

        ttk.Separator(cc, orient="horizontal").pack(fill="x", pady=10)

        # Poll interval
        interval_row = tk.Frame(cc, bg=BG_CARD)
        interval_row.pack(fill="x")
        tk.Label(interval_row, text="Poll interval (sec):", font=FONT_SMALL,
                 bg=BG_CARD, fg=FG_DIM).pack(side="left")
        self._wd_interval_entry = tk.Entry(interval_row, font=FONT_BODY,
                                            bg=BG_PANEL, fg=FG,
                                            insertbackground=FG, relief="flat", width=6)
        self._wd_interval_entry.insert(0, "120")
        self._wd_interval_entry.pack(side="left", padx=(8, 0))

        ttk.Separator(cc, orient="horizontal").pack(fill="x", pady=10)

        # Auto-actions
        self._wd_auto_stop = tk.BooleanVar(value=True)
        self._wd_auto_park = tk.BooleanVar(value=False)
        tk.Checkbutton(cc, text="Auto-stop sequence on NO-GO",
                       variable=self._wd_auto_stop,
                       bg=BG_CARD, fg=FG, selectcolor=BG_PANEL,
                       activebackground=BG_CARD, activeforeground=FG,
                       font=FONT_SMALL).pack(anchor="w", pady=2)
        tk.Checkbutton(cc, text="Auto-park telescope on NO-GO",
                       variable=self._wd_auto_park,
                       bg=BG_CARD, fg=FG, selectcolor=BG_PANEL,
                       activebackground=BG_CARD, activeforeground=FG,
                       font=FONT_SMALL).pack(anchor="w", pady=2)

        # ── Thresholds card (editable) ──
        tc = self._card(f, "Alert Thresholds  (edit and Save to apply)", 0, 1)
        tc.columnconfigure(1, weight=1)
        tc.columnconfigure(2, weight=0)

        def _thresh_row(label, row, default, unit):
            tk.Label(tc, text=label, font=FONT_SMALL,
                     bg=BG_CARD, fg=FG_DIM, anchor="w").grid(
                row=row, column=0, sticky="w", pady=3)
            entry = tk.Entry(tc, font=FONT_BODY, bg=BG_PANEL, fg=FG,
                             insertbackground=FG, relief="flat", width=8)
            entry.insert(0, str(default))
            entry.grid(row=row, column=1, sticky="w", padx=(8, 4), pady=3)
            tk.Label(tc, text=unit, font=FONT_SMALL,
                     bg=BG_CARD, fg=FG_DIM).grid(row=row, column=2, sticky="w")
            return entry

        self._wd_cloud_entry = _thresh_row("Cloud Cover Limit",  0,  60,   "%")
        self._wd_wind_entry  = _thresh_row("Wind Speed Limit",   1,  22,   "mph")
        self._wd_gusts_entry = _thresh_row("Wind Gust Limit",    2,  31,   "mph")
        self._wd_humid_entry = _thresh_row("Humidity Limit",     3,  90,   "%")
        self._wd_dew_entry   = _thresh_row("Dew Spread Limit",   4,  4.5,  "°F")
        self._wd_precip_entry= _thresh_row("Precipitation Limit",5,  0.005,"in")

        btn_row = tk.Frame(tc, bg=BG_CARD)
        btn_row.grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self._btn(btn_row, "💾 Save Thresholds", self._save_watchdog_thresholds,
                  ACCENT, 18).pack(side="left")
        self._wd_save_status = tk.Label(btn_row, text="", font=FONT_SMALL,
                                         bg=BG_CARD, fg=GO_COLOR)
        self._wd_save_status.pack(side="left", padx=(12, 0))

        # ── Alert log ──
        ac = self._card(f, "Alert Log", 1, 0, colspan=2)
        ac.columnconfigure(0, weight=1)
        ac.rowconfigure(0, weight=1)
        self._wd_log = scrolledtext.ScrolledText(
            ac, font=FONT_MONO, bg=BG_PANEL, fg=FG,
            insertbackground=FG, relief="flat", state="disabled")
        self._wd_log.grid(row=0, column=0, sticky="nsew")
        self._btn(ac, "Clear Log", lambda: self._set_text(self._wd_log, ""),
                  "#555577", 10).grid(row=1, column=0, sticky="w", pady=(6, 0))

    # ── ATLAS Chat Tab ───────────────────────────────────────────────────────

    def _build_atlas(self):
        f = self._tab_atlas
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)

        # Listening status bar
        self._listen_bar = tk.Frame(f, bg="#1a1a35", pady=6)
        self._listen_bar.grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 0))
        self._listen_bar.columnconfigure(1, weight=1)

        self._listen_indicator = tk.Label(
            self._listen_bar, text="⏸ Conversation paused",
            font=FONT_SMALL, bg="#1a1a35", fg=FG_DIM)
        self._listen_indicator.grid(row=0, column=0, padx=(10, 0))

        self._listen_toggle = tk.Button(
            self._listen_bar, text="▶ Start Conversation",
            command=self._toggle_listening,
            bg=GO_COLOR, fg="#ffffff", font=FONT_SMALL,
            relief="flat", cursor="hand2", padx=10, pady=3)
        self._listen_toggle.grid(row=0, column=2, padx=10)

        # Chat display
        self._chat_display = scrolledtext.ScrolledText(
            f, font=FONT_BODY, bg=BG_PANEL, fg=FG,
            insertbackground=FG, relief="flat", state="disabled",
            wrap="word")
        self._chat_display.grid(row=1, column=0, sticky="nsew",
                                padx=15, pady=(6, 0))

        # Tag colors
        self._chat_display.tag_config("atlas",  foreground=ACCENT)
        self._chat_display.tag_config("you",    foreground=GO_COLOR)
        self._chat_display.tag_config("system", foreground=FG_DIM)

        # Input area
        input_frame = tk.Frame(f, bg=BG, pady=8)
        input_frame.grid(row=2, column=0, sticky="ew", padx=15, pady=(6, 15))
        input_frame.columnconfigure(0, weight=1)

        self._chat_entry = tk.Entry(input_frame, font=FONT_BODY, bg=BG_PANEL,
                                    fg=FG, insertbackground=FG, relief="flat")
        self._chat_entry.grid(row=0, column=0, sticky="ew", ipady=6)
        self._chat_entry.bind("<Return>", lambda e: self._send_chat())

        self._btn(input_frame, "Send", self._send_chat, ACCENT, 8).grid(
            row=0, column=1, padx=(8, 0))
        self._mic_btn = self._btn(input_frame, "🎤 Once", self._start_voice, "#555577", 8)
        self._mic_btn.grid(row=0, column=2, padx=(4, 0))

        # Wire up continuous listener callbacks
        global _listen_callback, _listen_indicator_callback
        _listen_callback = self._on_voice_input
        _listen_indicator_callback = self._set_listen_indicator

        self._chat_append("ATLAS", "Observatory systems online. Say anything to talk to me, or click 'Start Conversation' for hands-free mode.")

    def _chat_append(self, speaker: str, text: str):
        self._chat_display.configure(state="normal")
        # Trim chat history to prevent unbounded memory growth
        line_count = int(self._chat_display.index("end-1c").split(".")[0])
        if line_count > 400:
            self._chat_display.delete("1.0", f"{line_count - 300}.0")
        timestamp = datetime.datetime.now().strftime("%I:%M %p")
        if speaker == "ATLAS":
            self._chat_display.insert("end", f"\n[{timestamp}] ATLAS: ", "atlas")
        elif speaker == "You":
            self._chat_display.insert("end", f"\n[{timestamp}] You: ", "you")
        else:
            self._chat_display.insert("end", f"\n[{timestamp}] {speaker}: ", "system")
        self._chat_display.insert("end", text)
        self._chat_display.configure(state="disabled")
        self._chat_display.see("end")

    # ── Actions ──────────────────────────────────────────────────────────────

    def _send_chat(self):
        msg = self._chat_entry.get().strip()
        if not msg:
            return
        self._chat_entry.delete(0, "end")
        self._chat_append("You", msg)
        threading.Thread(target=self._chat_worker, args=(msg,), daemon=True).start()

    def _chat_worker(self, msg: str):
        _chat_busy.set()
        try:
            result = api_post("/atlas/chat", {"message": msg}, timeout=60)
            reply  = result.get("reply", "No response from ATLAS.")
            self.after(0, lambda: self._chat_append("ATLAS", reply))
            self.after(0, lambda: speak(reply))
        finally:
            _chat_busy.clear()
            if _listening_active.is_set():
                self.after(0, lambda: self._listen_indicator.configure(
                    text="🎙 Listening..."))

    def _start_voice(self):
        label = "🎤 Loading..." if _whisper_model is None else "🎤 Listening..."
        self._mic_btn.configure(text=label, bg=NOGO_COLOR)
        self._chat_append("System", "Listening — speak now (up to 15 seconds, stops on silence)...")
        threading.Thread(target=self._voice_worker, daemon=True).start()

    def _voice_worker(self):
        text, error = record_and_transcribe()
        self.after(0, lambda: self._mic_btn.configure(text="🎤 Once", bg="#555577"))
        if error:
            self.after(0, lambda: self._chat_append("System", f"Voice error: {error}"))
        elif text:
            self.after(0, lambda: self._chat_entry.insert(0, text))
            self.after(0, self._send_chat)

    def _toggle_listening(self):
        if _listening_active.is_set():
            _listening_active.clear()
            self._listen_toggle.configure(text="▶ Start Conversation", bg=GO_COLOR)
            self._listen_indicator.configure(text="⏸ Conversation paused", fg=FG_DIM)
        else:
            _listening_active.set()
            self._listen_toggle.configure(text="■ Stop Conversation", bg=NOGO_COLOR)
            self._listen_indicator.configure(text="🎙 Listening...", fg=GO_COLOR)

    def _set_listen_indicator(self, msg: str):
        """Update the listening status label from any thread."""
        self.after(0, lambda: self._listen_indicator.configure(text=msg))

    def _on_voice_input(self, text: str):
        """Called by the continuous listener thread when speech is transcribed."""
        def _handle():
            if _chat_busy.is_set():
                self._listen_indicator.configure(text="⏳ ATLAS is responding...")
                return
            self._listen_indicator.configure(text="⏳ Asking ATLAS...")
            self._chat_append("You", text)
            threading.Thread(target=self._chat_worker, args=(text,), daemon=True).start()
        self.after(0, _handle)

    def _slew_to_target(self):
        target = self._slew_entry.get().strip()
        if not target:
            messagebox.showwarning("ATLAS", "Enter a target name.")
            return
        result = api_post(f"/telescope/slew?target_name={target}")
        self._log_session(f"Slew to {target}: {result}")

    def _park_telescope(self):
        result = api_post("/telescope/park")
        self._log_session(f"Park: {result}")

    def _unpark_telescope(self):
        result = api_post("/telescope/unpark")
        self._log_session(f"Unpark: {result}")

    def _move_focuser(self):
        try:
            pos = int(self._foc_entry.get())
        except ValueError:
            messagebox.showwarning("ATLAS", "Enter a valid integer position.")
            return
        result = api_post(f"/focuser/move?position={pos}")
        self._log_session(f"Focuser move to {pos}: {result}")

    def _start_guiding(self):
        result = api_post("/guiding/start")
        self._log_session(f"Start guiding: {result}")

    def _stop_guiding(self):
        result = api_post("/guiding/stop")
        self._log_session(f"Stop guiding: {result}")

    def _start_sequence(self):
        result = api_post("/sequence/start")
        self._log_session(f"Start sequence: {result}")

    def _stop_sequence(self):
        result = api_post("/sequence/stop")
        self._log_session(f"Stop sequence: {result}")

    def _pre_session_check(self):
        threading.Thread(target=self._pre_session_worker, daemon=True).start()

    def _pre_session_worker(self):
        self._log_session("Running pre-session check...")
        weather = api_get("/weather")
        moon    = api_get("/moon")
        status  = api_get("/status")
        msg = (f"Pre-session check:\n"
               f"  ATLAS verdict: {status.get('verdict')} — {status.get('reason')}\n"
               f"  Weather: {weather.get('verdict')} — {weather.get('reason')}\n"
               f"  Moon: {moon.get('phase_name')} ({moon.get('illumination_pct')}% illuminated)\n"
               f"  Cloud: {weather.get('cloud_cover_pct')}%  "
               f"Wind: {weather.get('wind_speed_mph')} mph  "
               f"Humidity: {weather.get('humidity_pct')}%")
        self._log_session(msg)
        speak(f"Pre-session check complete. {status.get('verdict')}. {status.get('reason')}")

    def _start_watchdog(self):
        # Apply current threshold settings before starting
        self._save_watchdog_thresholds(silent=True)
        result = api_post("/watchdog/start")
        self._log_session(f"Watchdog started: {result.get('status', result)}")

    def _stop_watchdog(self):
        result = api_post("/watchdog/stop")
        self._log_session(f"Watchdog stopped: {result.get('status', result)}")

    def _save_watchdog_thresholds(self, silent=False):
        threading.Thread(
            target=self._save_thresholds_worker, args=(silent,), daemon=True
        ).start()

    def _save_thresholds_worker(self, silent=False):
        try:
            thresholds = {
                "cloud_cover_limit_pct":  float(self._wd_cloud_entry.get()),
                "wind_speed_limit_mph":   float(self._wd_wind_entry.get()),
                "wind_gust_limit_mph":    float(self._wd_gusts_entry.get()),
                "humidity_limit_pct":     float(self._wd_humid_entry.get()),
                "dew_spread_limit_f":     float(self._wd_dew_entry.get()),
                "precip_limit_in":        float(self._wd_precip_entry.get()),
                "poll_interval_sec":      int(self._wd_interval_entry.get()),
                "auto_stop_sequence":     self._wd_auto_stop.get(),
                "auto_park_telescope":    self._wd_auto_park.get(),
            }
        except ValueError as e:
            if not silent:
                self.after(0, lambda: self._wd_save_status.configure(
                    text=f"Invalid value: {e}", fg=NOGO_COLOR))
            return
        result = api_post("/watchdog/thresholds", thresholds)
        if not silent:
            msg = "Saved." if "error" not in result else f"Error: {result['error']}"
            clr = GO_COLOR if "error" not in result else NOGO_COLOR
            self.after(0, lambda: self._wd_save_status.configure(text=msg, fg=clr))
            def _clear_status():
                try:
                    self._wd_save_status.configure(text="")
                except Exception:
                    pass
            self.after(3000, _clear_status)

    def _generate_session_plan(self):
        self._plan_btn.configure(state="disabled", text="⏳ Generating...")
        self._plan_status.configure(text="Asking ATLAS — this takes about 15 seconds...")
        self._set_text(self._plan_text, "")
        threading.Thread(target=self._session_plan_worker, daemon=True).start()

    def _session_plan_worker(self):
        result = api_post("/atlas/session-plan", timeout=60)
        plan = result.get("plan", "No plan returned.")
        generated = result.get("generated", "")
        timestamp = _fmt_datetime(generated) if generated else ""

        def update():
            self._set_text(self._plan_text, plan)
            self._plan_btn.configure(state="normal", text="✦ Generate Session Plan")
            self._plan_status.configure(
                text=f"Generated {timestamp}" if timestamp else "")
        self.after(0, update)

    def _lookup_object(self):
        name = self._lookup_entry.get().strip()
        if not name:
            return
        threading.Thread(target=self._lookup_worker, args=(name,), daemon=True).start()

    def _lookup_worker(self, name: str):
        weather = api_get("/weather")
        moon    = api_get("/moon")
        self._set_text(self._targets_text,
                       f"Looking up {name}...\nWeather: {weather.get('verdict')}\n"
                       f"Moon: {moon.get('phase_name')} {moon.get('illumination_pct')}%")

    def _refresh_forecast(self):
        threading.Thread(target=self._forecast_worker, daemon=True).start()

    def _forecast_worker(self):
        self._set_text(self._forecast_text, "Fetching forecast...")
        data = api_get("/weather/forecast?hours=24", timeout=15)
        if isinstance(data, list) and data:
            go   = sum(1 for h in data if h.get("verdict") == "GO")
            caut = sum(1 for h in data if h.get("verdict") == "CAUTION")
            nogo = sum(1 for h in data if h.get("verdict") == "NO-GO")
            lines = []
            lines.append(f"Next 24 hours:  GO={go}h   CAUTION={caut}h   NO-GO={nogo}h")
            lines.append("")
            lines.append(f"{'Time':<9} {'Verdict':<9} {'Cloud':>6} {'Wind':>7} {'Gusts':>7} {'Humid':>6} {'DewSprd':>8} {'Precip%':>8}")
            lines.append("─" * 72)
            for h in data:
                t      = _fmt_time(h.get("time", ""))
                verdict = h.get("verdict", "—")
                cloud  = h.get("cloud_cover_pct", 0)
                wind   = h.get("wind_speed_mph", 0)
                gusts  = h.get("wind_gusts_mph", 0)
                humid  = h.get("humidity_pct", 0)
                dew    = h.get("dew_spread_f", 0)
                precip = h.get("precip_probability_pct", 0)
                lines.append(
                    f"{t:<9} {verdict:<9} {cloud:>5.0f}%"
                    f" {wind:>6.1f}  {gusts:>5.1f}"
                    f"  {humid:>5.0f}%  {dew:>6.1f}°F  {precip:>6.0f}%"
                )
            self._set_text(self._forecast_text, "\n".join(lines))
        elif isinstance(data, dict) and "error" in data:
            self._set_text(self._forecast_text, f"Error: {data['error']}")
        else:
            self._set_text(self._forecast_text, "No forecast data returned.")

    def _refresh_targets(self):
        threading.Thread(target=self._targets_worker, daemon=True).start()

    def _targets_worker(self):
        self._set_text(self._targets_text, "Fetching tonight's best targets...")
        forecast = api_get("/weather/forecast?hours=12")
        if isinstance(forecast, list):
            go_hours = sum(1 for h in forecast if h.get("verdict") == "GO")
            text = f"Good imaging hours in next 12h: {go_hours}\n\n"
            text += f"{'Time':<9} {'Verdict':<10} {'Cloud':<8} {'Wind':<8} {'Humid':<8}\n"
            text += "─" * 50 + "\n"
            for h in forecast:
                t = _fmt_time(h.get("time", ""))
                text += (f"{t:<9} {h.get('verdict',''):<10} "
                         f"{h.get('cloud_cover_pct',0):<8.0f}% "
                         f"{h.get('wind_speed_mph',0):<8.1f} "
                         f"{h.get('humidity_pct',0):<8.0f}%\n")
            self._set_text(self._targets_text, text)
        else:
            self._set_text(self._targets_text, f"Error: {forecast}")

    def _force_status_refresh(self):
        threading.Thread(target=self._status_refresh_worker, daemon=True).start()

    def _status_refresh_worker(self):
        result = api_post("/status/refresh", timeout=30)
        verdict = result.get("verdict", "UNKNOWN")
        reason  = result.get("reason", "")
        updated = result.get("last_updated", "")
        self.after(0, lambda: self._update_banner(verdict, reason, updated))

    def _log_session(self, msg: str):
        timestamp = datetime.datetime.now().strftime("%I:%M:%S %p")
        self._append_text(self._session_log, f"[{timestamp}] {msg}\n")

    def _set_text(self, widget, text: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _append_text(self, widget, text: str, max_lines: int = 500):
        widget.configure(state="normal")
        widget.insert("end", text)
        # Trim to prevent unbounded memory growth
        line_count = int(widget.index("end-1c").split(".")[0])
        if line_count > max_lines + 100:
            widget.delete("1.0", f"{line_count - max_lines}.0")
        widget.see("end")
        widget.configure(state="disabled")

    # ── Polling ──────────────────────────────────────────────────────────────

    def _start_polling(self):
        self._poll_status()
        self._poll_telescope()
        self._poll_camera()
        self._poll_guiding()
        self._poll_weather()
        self._poll_forecast()
        self._poll_watchdog()
        self._poll_moon()

    def _poll_status(self):
        if self._poll_running.get("status"):
            self.after(30_000, self._poll_status)
            return
        self._poll_running["status"] = True
        def worker():
            try:
                data = api_get("/status")
                verdict = data.get("verdict", "UNKNOWN")
                reason  = data.get("reason", "No data")
                updated = data.get("last_updated", "")
                self.after(0, lambda: self._update_banner(verdict, reason, updated))
            finally:
                self._poll_running["status"] = False
        threading.Thread(target=worker, daemon=True).start()
        self.after(30_000, self._poll_status)

    def _poll_telescope(self):
        if self._poll_running.get("telescope"):
            self.after(5_000, self._poll_telescope)
            return
        self._poll_running["telescope"] = True
        def worker():
            try:
                data = api_get("/telescope")
                if "error" not in data:
                    connected = data.get("Connected", False)
                    ra  = data.get("RightAscension", 0)
                    dec = data.get("Declination", 0)
                    alt = data.get("Altitude", 0)
                    az  = data.get("Azimuth", 0)

                    def update():
                        status = "Connected" if connected else "Disconnected"
                        color  = FG if connected else NOGO_COLOR
                        self._tel_connected.configure(text=status, fg=color)
                        self._ov_tel_status.configure(text=status, fg=color)
                        ra_str = f"{ra:.4f}°"
                        self._tel_ra.configure(text=ra_str)
                        self._ov_tel_ra.configure(text=ra_str)
                        dec_str = f"{dec:.4f}°"
                        self._tel_dec.configure(text=dec_str)
                        self._ov_tel_dec.configure(text=dec_str)
                        alt_str = f"{alt:.1f}°"
                        self._tel_alt.configure(text=alt_str)
                        self._ov_tel_alt.configure(text=alt_str)
                        self._tel_az.configure(text=f"{az:.1f}°")
                        self._tel_tracking.configure(
                            text="Yes" if data.get("Tracking") else "No")
                        self._tel_slewing.configure(
                            text="Yes" if data.get("Slewing") else "No")
                        self._tel_pier.configure(text=data.get("SideOfPier", "—"))
                    self.after(0, update)
            finally:
                self._poll_running["telescope"] = False
        threading.Thread(target=worker, daemon=True).start()
        self.after(5_000, self._poll_telescope)

    def _poll_camera(self):
        if self._poll_running.get("camera"):
            self.after(5_000, self._poll_camera)
            return
        self._poll_running["camera"] = True
        def worker():
            try:
                data = api_get("/camera")
                if "error" not in data:
                    def update():
                        connected = data.get("Connected", False)
                        self._cam_connected.configure(
                            text="Connected" if connected else "Disconnected",
                            fg=FG if connected else NOGO_COLOR)
                        self._cam_name.configure(text=data.get("Name", "—"))
                        temp = data.get("Temperature")
                        self._cam_temp.configure(
                            text=f"{temp:.1f}°C" if temp is not None else "—")
                        self._ov_cam_temp.configure(
                            text=f"{temp:.1f}°C" if temp is not None else "—")
                        self._cam_gain.configure(text=str(data.get("Gain", "—")))
                        self._cam_offset.configure(text=str(data.get("Offset", "—")))
                        binning = data.get("BinX", 1)
                        self._cam_binning.configure(text=f"{binning}x{binning}")
                        exposing = data.get("IsExposing", False)
                        self._cam_exposing.configure(
                            text="Yes" if exposing else "No",
                            fg=GO_COLOR if exposing else FG)
                        self._ov_cam_exp.configure(
                            text="Yes" if exposing else "No",
                            fg=GO_COLOR if exposing else FG)
                        self._cam_exposure.configure(
                            text=f"{data.get('LastExposureDuration', '—')}s")
                    self.after(0, update)
            finally:
                self._poll_running["camera"] = False
        threading.Thread(target=worker, daemon=True).start()
        self.after(5_000, self._poll_camera)

    def _poll_guiding(self):
        if self._poll_running.get("guiding"):
            self.after(5_000, self._poll_guiding)
            return
        self._poll_running["guiding"] = True
        def worker():
            try:
                state = api_get("/guiding/state")
                stats = api_get("/guiding/stats")

                def update():
                    s = state.get("result", "—")
                    self._guide_state.configure(text=s)
                    self._ov_guide_state.configure(text=s)

                    if "result" in stats:
                        r = stats["result"]
                        rms_total = r.get("rms_total", 0)
                        rms_ra    = r.get("rms_ra", 0)
                        rms_dec   = r.get("rms_dec", 0)
                        self._guide_rms.configure(text=f"{rms_total:.2f}\"")
                        self._guide_rms_ra.configure(text=f"{rms_ra:.2f}\"")
                        self._guide_rms_dec.configure(text=f"{rms_dec:.2f}\"")
                        self._ov_guide_rms.configure(text=f"{rms_total:.2f}\"")
                        self._ov_guide_ra.configure(text=f"{rms_ra:.2f}\"")
                        self._ov_guide_dec.configure(text=f"{rms_dec:.2f}\"")
                        self._guide_peak_ra.configure(
                            text=f"{r.get('peak_ra', 0):.2f}\"")
                        self._guide_peak_dec.configure(
                            text=f"{r.get('peak_dec', 0):.2f}\"")
                        self._guide_snr.configure(
                            text=f"{r.get('snr', 0):.1f}")

                        # Update graph — draw_idle() is safe to call from UI thread
                        if self._guide_canvas:
                            self._guide_ra_data.append(rms_ra)
                            self._guide_dec_data.append(rms_dec)
                            if len(self._guide_ra_data) > 120:
                                self._guide_ra_data.pop(0)
                                self._guide_dec_data.pop(0)
                            x = list(range(len(self._guide_ra_data)))
                            self._guide_ra_line.set_data(x, self._guide_ra_data)
                            self._guide_dec_line.set_data(x, self._guide_dec_data)
                            self._guide_ax.relim()
                            self._guide_ax.autoscale_view()
                            self._guide_canvas.draw_idle()
                self.after(0, update)
            finally:
                self._poll_running["guiding"] = False
        threading.Thread(target=worker, daemon=True).start()
        self.after(5_000, self._poll_guiding)

    def _poll_weather(self):
        if self._poll_running.get("weather"):
            self.after(60_000, self._poll_weather)
            return
        self._poll_running["weather"] = True
        def worker():
            try:
                data = api_get("/weather")
                if "error" not in data:
                    def update():
                        v = data.get("verdict", "—")
                        vc = {"GO": GO_COLOR, "CAUTION": CAUTION_COLOR,
                              "NO-GO": NOGO_COLOR}.get(v, FG)
                        self._wx_verdict.configure(text=v, fg=vc)
                        self._wx_reason.configure(text=data.get("reason", "—"))
                        self._wx_cloud.configure(
                            text=f"{data.get('cloud_cover_pct', '—')}%")
                        self._ov_wx_cloud.configure(
                            text=f"{data.get('cloud_cover_pct', '—')}%")
                        self._wx_temp.configure(
                            text=f"{data.get('temperature_f', '—')}°F")
                        self._wx_humid.configure(
                            text=f"{data.get('humidity_pct', '—')}%")
                        self._ov_wx_humid.configure(
                            text=f"{data.get('humidity_pct', '—')}%")
                        self._wx_dew.configure(
                            text=f"{data.get('dew_point_f', '—')}°F")
                        spread = data.get("temperature_f", 70) - data.get("dew_point_f", 60) \
                            if data.get("temperature_f") and data.get("dew_point_f") else None
                        self._wx_spread.configure(
                            text=f"{spread:.1f}°F" if spread else "—")
                        self._ov_wx_dew.configure(
                            text=f"{spread:.1f}°F" if spread else "—")
                        self._wx_wind.configure(
                            text=f"{data.get('wind_speed_mph', '—')} mph")
                        self._ov_wx_wind.configure(
                            text=f"{data.get('wind_speed_mph', '—')} mph")
                        self._wx_gusts.configure(
                            text=f"{data.get('wind_gusts_mph', '—')} mph")
                        self._wx_precip.configure(
                            text=f"{data.get('precipitation_in', '—')}\"")
                        self._wx_pressure.configure(
                            text=f"{data.get('pressure_hpa', '—')} hPa")
                    self.after(0, update)
            finally:
                self._poll_running["weather"] = False
        threading.Thread(target=worker, daemon=True).start()
        self.after(60_000, self._poll_weather)

    def _poll_forecast(self):
        threading.Thread(target=self._forecast_worker, daemon=True).start()
        self.after(1_800_000, self._poll_forecast)  # refresh every 30 minutes

    def _poll_watchdog(self):
        if self._poll_running.get("watchdog"):
            self.after(15_000, self._poll_watchdog)
            return
        self._poll_running["watchdog"] = True
        def worker():
            try:
                data = api_get("/watchdog")
                if "error" not in data:
                    def update():
                        running = data.get("enabled", False)
                        self._wd_status_label.configure(
                            text="● RUNNING" if running else "● STOPPED",
                            fg=GO_COLOR if running else NOGO_COLOR)
                        self._ov_watchdog.configure(
                            text="Running" if running else "Stopped",
                            fg=GO_COLOR if running else FG_DIM)

                        # Populate editable entries on first load only
                        if not self._wd_thresholds_loaded:
                            self._wd_thresholds_loaded = True
                            def _set(entry, val):
                                entry.delete(0, "end")
                                entry.insert(0, str(val))
                            _set(self._wd_cloud_entry,  data.get("cloud_cover_limit_pct", 60))
                            _set(self._wd_wind_entry,   data.get("wind_speed_limit_mph",  22))
                            _set(self._wd_gusts_entry,  data.get("wind_gust_limit_mph",   31))
                            _set(self._wd_humid_entry,  data.get("humidity_limit_pct",    90))
                            _set(self._wd_dew_entry,    data.get("dew_spread_limit_f",   4.5))
                            _set(self._wd_precip_entry, data.get("precip_limit_in",     0.005))
                            _set(self._wd_interval_entry, data.get("poll_interval_sec",  120))
                            self._wd_auto_stop.set(data.get("auto_stop_sequence",  True))
                            self._wd_auto_park.set(data.get("auto_park_telescope", False))

                        # Alert log — append new alerts
                        alerts = data.get("alerts", [])
                        if alerts:
                            log_text = "\n".join(
                                f"[{_fmt_datetime(a['time'])}]  {a['reason']}"
                                for a in alerts[-50:])
                            self._set_text(self._wd_log, log_text)
                    self.after(0, update)
            finally:
                self._poll_running["watchdog"] = False
        threading.Thread(target=worker, daemon=True).start()
        self.after(15_000, self._poll_watchdog)

    def _poll_moon(self):
        def worker():
            data = api_get("/moon")
            if "error" not in data:
                def update():
                    self._plan_moon_phase.configure(text=data.get("phase_name", "—"))
                    self._plan_moon_illum.configure(
                        text=f"{data.get('illumination_pct', '—')}%")
                    self._ov_moon.configure(
                        text=f"{data.get('phase_name', '—')} "
                             f"({data.get('illumination_pct', '—')}%)")
                self.after(0, update)
        threading.Thread(target=worker, daemon=True).start()
        self.after(3_600_000, self._poll_moon)

    def _update_clock(self):
        now = datetime.datetime.now()
        utc = datetime.datetime.now(datetime.timezone.utc)
        self._clock_label.configure(
            text=f"Local {now.strftime('%I:%M:%S %p')}  |  UTC {utc.strftime('%I:%M:%S %p')}")
        self.after(1_000, self._update_clock)


# ---------------------------------------------------------------------------
# First-run config dialog
# ---------------------------------------------------------------------------
class ConfigDialog(tk.Tk):
    def __init__(self, config: dict):
        super().__init__()
        self.title("ATLAS — First Time Setup")
        self.geometry("480x380")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.result = None
        self._cfg = dict(config)
        self._build()

    def _build(self):
        tk.Label(self, text="ATLAS Setup", font=FONT_TITLE,
                 bg=BG, fg=ACCENT).pack(pady=(20, 4))
        tk.Label(self, text="Enter your observatory network settings",
                 font=FONT_BODY, bg=BG, fg=FG_DIM).pack(pady=(0, 20))

        form = tk.Frame(self, bg=BG)
        form.pack(padx=40, fill="x")

        def field(label, key, row, default=""):
            tk.Label(form, text=label, font=FONT_SMALL,
                     bg=BG, fg=FG_DIM, anchor="w").grid(
                row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=self._cfg.get(key, default))
            entry = tk.Entry(form, textvariable=var, font=FONT_BODY,
                             bg=BG_PANEL, fg=FG, insertbackground=FG,
                             relief="flat", width=28)
            entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
            form.columnconfigure(1, weight=1)
            return var

        self._v_obs_ip   = field("Observatory PC IP:", "observatory_ip",   0, "192.168.1.100")
        self._v_obs_name = field("Observatory Name:",  "observatory_name", 1, "My Observatory")
        self._v_lat      = field("Latitude (°N):",     "obs_lat",          2, "0.0")
        self._v_lon      = field("Longitude (°E):",    "obs_lon",          3, "0.0")

        tk.Label(self, text="Longitude is negative for West (e.g. -82.06 for Florida)",
                 font=FONT_SMALL, bg=BG, fg=FG_DIM).pack(pady=(4, 0))

        tk.Button(self, text="Save & Launch ATLAS", command=self._save,
                  bg=ACCENT, fg="#ffffff", font=FONT_HEAD,
                  relief="flat", cursor="hand2", pady=8).pack(pady=20, padx=40, fill="x")

    def _save(self):
        try:
            self.result = {
                "observatory_ip":   self._v_obs_ip.get().strip(),
                "observatory_name": self._v_obs_name.get().strip(),
                "obs_lat":          float(self._v_lat.get()),
                "obs_lon":          float(self._v_lon.get()),
            }
            self.destroy()
        except ValueError:
            messagebox.showerror("Error", "Latitude and longitude must be numbers.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    config = load_config()

    # Show setup dialog on first run or if IP not configured
    if config.get("observatory_ip") == DEFAULT_CONFIG["observatory_ip"] or \
       not CONFIG_FILE.exists():
        dlg = ConfigDialog(config)
        dlg.mainloop()
        if dlg.result:
            save_config(dlg.result)
            CONFIG.update(dlg.result)
            SERVER = f"http://{CONFIG['observatory_ip']}:5000"

    app = ATLASDashboard()
    app.mainloop()
