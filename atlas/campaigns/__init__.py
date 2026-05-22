"""Campaign continuity — multi-night progress + resume logic."""
from atlas.campaigns.continuity import (
    TargetProgress, CampaignProgress,
    target_progress, campaign_progress,
    is_campaign_done, next_filter_priority,
)

__all__ = [
    "TargetProgress", "CampaignProgress",
    "target_progress", "campaign_progress",
    "is_campaign_done", "next_filter_priority",
]
