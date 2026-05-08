# ATLAS Setup Guide

## Overview

ATLAS consists of two components:
- **ATLAS Server** — runs on your observatory PC
- **ATLAS Dashboard** — runs on your warm room PC

Both connect over your local network.

---

## Prerequisites

### Observatory PC
1. **NINA** installed with Advanced API enabled
   - In NINA: Options → Advanced → Enable Advanced API → Port 1888
2. **PHD2** installed with server enabled
   - In PHD2: Tools → Enable Server
3. **Ollama** installed with Qwen2.5:7b
   - Download from https://ollama.com
   - Run: `ollama pull qwen2.5:7b`
4. Static IP address assigned in your router for this PC

### Warm Room PC
1. Network access to the observatory PC
2. Microphone (for voice interface)

---

## Installation

### Step 1 — Observatory PC
1. Run `ATLAS_Server_Setup_v1.0.0.exe`
2. When prompted, enter:
   - **Observatory PC IP Address** — the static IP of this machine
   - **Observatory Name** — e.g. "Silver Springs Observatory"
   - **Latitude** — decimal degrees North
   - **Longitude** — decimal degrees (negative for West)
3. Complete installation
4. ATLAS Server will start automatically

### Step 2 — Warm Room PC
1. Run `ATLAS_Dashboard_Setup_v1.0.0.exe`
2. When prompted, enter:
   - **Observatory PC IP Address** — the static IP of the observatory PC
   - **Observatory Name** — same as above
   - **Latitude/Longitude** — same as above
3. Complete installation
4. ATLAS Dashboard will launch

---

## Network Requirements

Both PCs must be on the same local network (same router/subnet).

The observatory PC must have a **static IP address**. Set this in your router's DHCP reservation table using the PC's MAC address.

Firewall: Windows Firewall may block port 5000 on the observatory PC.
To allow it:
```
netsh advfirewall firewall add rule name="ATLAS Server" dir=in action=allow protocol=TCP localport=5000
```

---

## NINA Advanced API Setup

In NINA:
1. Options (gear icon) → Advanced
2. Scroll to "Advanced API"
3. Enable: ✓
4. Port: 1888
5. Click Save

---

## PHD2 Server Setup

In PHD2:
1. Tools menu → Enable Server
2. Port: 4400 (default)

---

## Voice Interface

The ATLAS voice interface uses:
- **Whisper** (OpenAI) for speech-to-text — runs locally, no internet needed
- **Ollama + Qwen2.5:7b** for AI responses — runs locally, no internet needed
- **Windows SAPI** for text-to-speech — built into Windows

Click the **🎤 Speak** button in the ATLAS tab and speak naturally.
ATLAS will respond via voice and text.

---

## Troubleshooting

**Dashboard shows "Connecting to observatory..."**
- Check that atlas_server.exe is running on the observatory PC
- Verify the IP address in config.json matches the observatory PC
- Check Windows Firewall on the observatory PC (port 5000)

**ATLAS verdict shows UNKNOWN**
- Check that Ollama is running: `ollama serve`
- Verify Qwen2.5:7b is installed: `ollama list`

**Guiding stats not updating**
- Verify PHD2 server is enabled (Tools → Enable Server)
- Check PHD2 is running on the observatory PC

**Camera shows Disconnected**
- Connect camera in NINA before starting ATLAS Server
