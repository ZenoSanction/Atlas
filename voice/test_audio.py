import sounddevice as sd
import pyttsx3

print("=== Output Devices ===")
devices = sd.query_devices()
default_out = sd.default.device[1]
for d in devices:
    if d["max_output_channels"] > 0:
        marker = " << DEFAULT" if d["index"] == default_out else ""
        print(f"  [{d['index']}] {d['name']}{marker}")

print(f"\nDefault output device index: {default_out}")
print("\n=== TTS Voices ===")
engine = pyttsx3.init()
for i, v in enumerate(engine.getProperty("voices")):
    print(f"  [{i}] {v.name}")

print("\nSpeaking test phrase now...")
engine.say("ATLAS online. If you can hear this, audio is working correctly.")
engine.runAndWait()
print("Speech complete.")
