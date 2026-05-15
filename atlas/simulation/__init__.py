"""Simulation mode — fake hardware for shakedown testing without commanding
real equipment. Per Round 4 #21.

When ``Settings.simulation_mode`` is True, hardware client constructors are
substituted with the FakeNina / FakePhd2 variants below.
"""
from atlas.simulation.fake_hardware import FakeNina, FakePhd2

__all__ = ["FakeNina", "FakePhd2"]
