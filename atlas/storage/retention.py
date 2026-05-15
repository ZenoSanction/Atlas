"""Retention policy enforcement.

Per Round 4 #24:
- Raw subs: 90 days (default), then candidate for deletion
- Planetary video: 30 days
- Session reports, calibration masters, references, submissions: forever
- Cleanup preview before any deletion runs

Phase 1 ships the scan + preview. Actual deletion is gated behind operator
approval (preview shown in dashboard, click to confirm).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from atlas.config import get_settings
from atlas.db.managers import ConfigManager
from atlas.logging_setup import get_logger

log = get_logger("storage.retention")


@dataclass
class CleanupCandidate:
    path: Path
    size_bytes: int
    category: str    # "raw_sub" | "planetary_video"
    older_than_days: int


@dataclass
class CleanupPreview:
    candidates: list[CleanupCandidate] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(c.size_bytes for c in self.candidates)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / 1_000_000_000


class RetentionEngine:
    def preview(self) -> CleanupPreview:
        s = get_settings()
        policy = ConfigManager.get_retention()
        result = CleanupPreview()

        # Raw subs
        if policy.raw_subs_days > 0:
            cutoff = datetime.utcnow() - timedelta(days=policy.raw_subs_days)
            for p in s.frames_dir.rglob("*.fit*"):
                if not p.is_file():
                    continue
                try:
                    mtime = datetime.utcfromtimestamp(p.stat().st_mtime)
                except OSError:
                    continue
                if mtime < cutoff:
                    age = (datetime.utcnow() - mtime).days
                    result.candidates.append(CleanupCandidate(
                        path=p, size_bytes=p.stat().st_size,
                        category="raw_sub", older_than_days=age,
                    ))

        # Planetary video
        if policy.planetary_video_days > 0:
            cutoff = datetime.utcnow() - timedelta(days=policy.planetary_video_days)
            for ext in ("*.ser", "*.avi", "*.mov"):
                for p in s.frames_dir.rglob(ext):
                    if not p.is_file():
                        continue
                    try:
                        mtime = datetime.utcfromtimestamp(p.stat().st_mtime)
                    except OSError:
                        continue
                    if mtime < cutoff:
                        age = (datetime.utcnow() - mtime).days
                        result.candidates.append(CleanupCandidate(
                            path=p, size_bytes=p.stat().st_size,
                            category="planetary_video", older_than_days=age,
                        ))

        return result

    def execute(self, preview: CleanupPreview) -> tuple[int, int]:
        """Delete the candidates in ``preview``. Returns (n_deleted, n_failed)."""
        deleted = failed = 0
        for c in preview.candidates:
            try:
                c.path.unlink(missing_ok=True)
                deleted += 1
            except OSError as e:
                log.warning("Failed to delete %s: %s", c.path, e)
                failed += 1
        log.info("retention executed: deleted=%d failed=%d", deleted, failed)
        return deleted, failed
