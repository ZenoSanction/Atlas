"""Idempotent seeder for the bench-test campaign.

Creates a "Bench-test campaign" if absent and pre-links a handful of
well-placed bright targets so the Planner has something concrete to
schedule the moment the operator powers up — instead of falling back
to the seasonal showcase catalog.

This is the on-ramp for the bench-hardware weekend: by the time the
camera, mount, and PHD2 come online, the Plan tab already shows a
real campaign with named targets the operator can confidently slew to.

Idempotent: re-running this is safe. The campaign is keyed by exact
name match. Targets are upserted by name + linked only if the link
doesn't already exist. Re-seeding after the operator has manually
removed a target won't re-add it (the operator's decision wins).
"""
from __future__ import annotations

from dataclasses import dataclass

from atlas.db.managers import CampaignManager, TargetManager
from atlas.db.models import CampaignStatus, WorkflowKind
from atlas.logging_setup import get_logger

log = get_logger("db.seed_bench")

BENCH_CAMPAIGN_NAME = "Bench-test campaign"


@dataclass(frozen=True)
class _SeedTarget:
    name: str
    object_type: str
    ra_deg: float
    dec_deg: float
    magnitude: float
    aliases: tuple[str, ...] = ()


# Four well-placed targets for a spring/summer evening at ~32 N:
#   * Vega       — globally one of the brightest stars, near zenith
#                  through summer evenings. Good for focus + first light.
#   * Arcturus   — bright orange giant, high in the spring evening sky.
#                  Excellent for tracking + plate-solve smoke tests.
#   * M13        — globular cluster in Hercules; visible all night in
#                  May/June. A real astrophotography target.
#   * M5         — bright globular in Serpens; rises right after dusk
#                  in May, well placed by midnight.
_BENCH_TARGETS: tuple[_SeedTarget, ...] = (
    _SeedTarget(name="Vega", object_type="star",
                ra_deg=279.235, dec_deg=38.784, magnitude=0.03,
                aliases=("alpha Lyr", "Alpha Lyrae")),
    _SeedTarget(name="Arcturus", object_type="star",
                ra_deg=213.915, dec_deg=19.182, magnitude=-0.05,
                aliases=("alpha Boo", "Alpha Bootis")),
    _SeedTarget(name="M13", object_type="globular_cluster",
                ra_deg=250.422, dec_deg=36.460, magnitude=5.8,
                aliases=("Great Hercules Cluster", "NGC 6205")),
    _SeedTarget(name="M5", object_type="globular_cluster",
                ra_deg=229.638, dec_deg=2.081, magnitude=5.7,
                aliases=("NGC 5904",)),
)


def seed_bench_campaign() -> dict:
    """Create + populate the bench-test campaign. Returns a summary dict
    suitable for direct JSON response from the API route."""
    # Find the campaign (case-sensitive exact name match)
    campaign_id = None
    for c in CampaignManager.list_all():
        if c.name == BENCH_CAMPAIGN_NAME:
            campaign_id = c.id
            break
    created_campaign = False
    if campaign_id is None:
        campaign_id = CampaignManager.create(
            name=BENCH_CAMPAIGN_NAME,
            workflow=WorkflowKind.DEEPSKY,
            priority=70,
            proposed_by="operator",
            cadence="every_clear_night",
            scientific_context=(
                "Bench-hardware smoke test campaign — bright, easy targets "
                "for first-light validation of the mount + camera + PHD2 + "
                "ASTAP pipeline. Replace or pause once real science "
                "campaigns are loaded."
            ),
        )
        created_campaign = True
        log.info("Created %r (id=%d)", BENCH_CAMPAIGN_NAME, campaign_id)

    CampaignManager.set_status(campaign_id, CampaignStatus.ACTIVE)

    added_targets: list[str] = []
    already_linked: list[str] = []
    for spec in _BENCH_TARGETS:
        tid = TargetManager.upsert(
            name=spec.name,
            object_type=spec.object_type,
            ra_deg=spec.ra_deg, dec_deg=spec.dec_deg,
            magnitude=spec.magnitude,
            aliases=list(spec.aliases) if spec.aliases else None,
        )
        if TargetManager.link_to_campaign(campaign_id, tid):
            added_targets.append(spec.name)
        else:
            already_linked.append(spec.name)
    log.info("Seeded bench campaign: created_campaign=%s, added=%s, "
              "already_linked=%s", created_campaign, added_targets,
              already_linked)
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "campaign_name": BENCH_CAMPAIGN_NAME,
        "created_campaign": created_campaign,
        "added_targets": added_targets,
        "already_linked_targets": already_linked,
        "total_targets": len(added_targets) + len(already_linked),
    }
