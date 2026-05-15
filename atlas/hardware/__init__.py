"""Hardware abstraction layer.

NINA is the primary control surface — ATLAS does not talk to ASCOM/INDI
directly. Anything NINA supports, ATLAS supports.

Modules:
    nina        NINA Advanced API client (HTTP, port 1888)
    phd2        PHD2 JSON-RPC client (TCP, port 4400)
    astap       ASTAP plate solver wrapper (subprocess + result parse)
    power       OS-level power source detection
    interface   Abstract base for simulation-mode hot-swap
"""
from atlas.hardware.nina import NinaClient, NinaError
from atlas.hardware.phd2 import Phd2Client, Phd2Error
from atlas.hardware.power import PowerMonitor

__all__ = ["NinaClient", "NinaError", "Phd2Client", "Phd2Error", "PowerMonitor"]
