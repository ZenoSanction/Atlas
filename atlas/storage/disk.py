"""Disk usage monitoring."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from atlas.config import get_settings
from atlas.db.managers import StorageEventManager
from atlas.logging_setup import get_logger

log = get_logger("storage.disk")


@dataclass
class DiskSnapshot:
    path: Path
    bytes_total: int
    bytes_free: int
    bytes_used_by_atlas: int

    @property
    def percent_used(self) -> float:
        if self.bytes_total == 0:
            return 0.0
        return 100.0 * (1.0 - self.bytes_free / self.bytes_total)

    @property
    def gb_free(self) -> float:
        return self.bytes_free / 1_000_000_000


class DiskMonitor:
    def __init__(self, watch_path: Path | None = None) -> None:
        self._path = watch_path or get_settings().install_root

    def snapshot(self, record: bool = True) -> DiskSnapshot:
        total, _used, free = shutil.disk_usage(str(self._path))
        atlas_used = self._sum_atlas_dirs()
        snap = DiskSnapshot(path=self._path, bytes_total=total,
                             bytes_free=free, bytes_used_by_atlas=atlas_used)
        if record:
            StorageEventManager.record(
                bytes_total=total, bytes_free=free,
                bytes_used_by_atlas=atlas_used,
            )
        return snap

    def _sum_atlas_dirs(self) -> int:
        s = get_settings()
        total = 0
        for p in (s.frames_dir, s.references_dir, s.reports_dir, s.logs_dir,
                  s.data_dir):
            if not p.exists():
                continue
            for f in p.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        return total
