"""
Simulates exactly what happens in the voice loop:
InputStream running + speak() called at the same time.
"""
import os, tempfile, time, winsound, queue, threading
import numpy as np
import sounddevice as sd
import pyttsx3

SAMPLE_RATE  = 16000
CHUNK_FRAMES = 800

audio_q = queue.Queue()

def audio_callback(indata, frames, time_info, status):
    audio_q.put(indata.copy().flatten())

engine = pyttsx3.init()
engine.setProperty("rate", 160)

def speak(text):
    print(f"speak() called with: {text[:40]}", flush=True)
    tmp = tempfile.mktemp(suffix=".wav")
    print(f"  Saving to {tmp}...", flush=True)
    engine.save_to_file(text, tmp)
    engine.runAndWait()
    size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    print(f"  WAV file size: {size} bytes", flush=True)
    print(f"  Playing via winsound...", flush=True)
    winsound.PlaySound(tmp, winsound.SND_FILENAME)
    print(f"  winsound done.", flush=True)
    os.unlink(tmp)

print("Starting InputStream (simulating live mic)...")
with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                    blocksize=CHUNK_FRAMES, callback=audio_callback):
    print("InputStream running. Calling speak() now...")
    speak("ATLAS online. This is a full simulation test. Can you hear me?")
    print("Test complete.")
