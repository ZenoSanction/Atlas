# ATLAS — Automated Telescope & Long-term Astronomy System

ATLAS is an autonomous observatory control system consisting of two components:

- **atlas_server** — runs on the observatory PC alongside NINA and PHD2. Exposes all observatory functions as a REST API over the local network.
- **atlas_dashboard** — runs on the warm room PC. A full-featured desktop control panel with live telescope, camera, guiding, weather, and session status. Includes voice interface powered by local AI (Ollama/Qwen2.5).

## Requirements

### Observatory PC
- Windows 10/11
- NINA (with Advanced API enabled on port 1888)
- PHD2 (with server enabled on port 4400)
- Ollama with Qwen2.5:7b installed
- Python 3.11+

### Warm Room PC
- Windows 10/11
- Network access to the observatory PC
- Python 3.11+ (or use the standalone .exe installer)
- Microphone (for voice interface)

## Quick Start

See [docs/setup.md](docs/setup.md) for full installation instructions.

## License

MIT License — free to use, modify, and distribute.
